"""AS.1.5 — OAuth token-shape drift guard (Python ↔ TS twin).

Behavioural drift guard between
:func:`backend.security.oauth_client.parse_token_response` (Python) and
``parseTokenResponse`` exported from
``templates/_shared/oauth-client/index.ts`` (TS twin).

Why this test exists
────────────────────
AS.1.2's existing cross-twin drift guards are **static** — SHA-256
oracles over the 5 canonical event strings + value-equality on the 4
numeric defaults, parsed out of the TS source via regex. Those catch
*constant* drift (someone renames an audit event on one side only) but
NOT *behavioural* drift (someone changes the scope-parser regex on one
side, both still ship 5 event strings + 4 numeric defaults but produce
different ``TokenSet`` shapes from the same vendor JSON).

AS.1.5 closes that gap: real vendor-shaped token responses are pushed
through both parsers and the resulting ``TokenSet`` shape (excluding
implementation-specific names — ``access_token`` ↔ ``accessToken`` —
which we normalise) MUST match byte-for-byte, including:

    * ``access_token`` / ``refresh_token`` / ``id_token`` string identity
    * ``token_type`` default-fill ("Bearer" when missing)
    * ``expires_at`` absolute-timestamp arithmetic with shared ``now``
    * ``scope`` parser parity across the four input forms vendors actually
      ship (space-separated string / comma-separated string / JSON list /
      missing field)
    * ``raw`` field preservation (the verbatim provider response stays
      attached for callers that need vendor-specific extras)

Coverage shape
──────────────
1. **Per-vendor real-shape fixtures** (11 fixtures, one per AS.1.3
   catalog vendor). Each fixture mirrors what the vendor actually
   returns from its token endpoint as of the AS.1.3 row landing date
   (vendor docs cited in :mod:`backend.security.oauth_vendors`):

       * ``github``      — minimal text/json shape, comma-separated scope
       * ``google``      — full OIDC shape, id_token + refresh_token
       * ``microsoft``   — Entra ID v2.0 shape, scope as space-separated
       * ``apple``       — id_token + refresh_token, *no* scope echo
       * ``gitlab``      — OIDC + ``created_at`` extra surfaced via raw
       * ``bitbucket``   — Cloud shape, ``state`` field surfaced via raw
       * ``slack``       — v2 shape with nested ``authed_user`` extra
       * ``notion``      — workspace-scoped, no expires_in, no refresh
       * ``salesforce``  — many extras (``instance_url``, ``signature``)
       * ``hubspot``     — minimal, no ``token_type`` echoed (defaults)
       * ``discord``     — standard shape

2. **Quirk fixtures** (5 fixtures) covering parser-edge cases that
   exposed historical regressions:

       * ``scope_comma``        — comma-separated scope dedupe
       * ``scope_list``         — JSON list scope passthrough
       * ``scope_dedupe``       — duplicate tokens in scope string
       * ``string_expires_in``  — expires_in arrives as a JSON string
       * ``zero_expires_in``    — expires_in = 0 (boundary)

3. **Negative fixtures** (3 fixtures). Both parsers MUST reject:

       * ``error_shape``        — RFC 6749 §5.2 error payload
       * ``missing_access``     — no ``access_token`` field
       * ``negative_expires``   — expires_in = -1 (vendor bug)

   We assert that *both sides raise* — message-string comparison is
   deliberately skipped (Python uses ``repr()`` for description while TS
   uses ``JSON.stringify``, both fine, both not part of the contract).

How TS execution works
──────────────────────
Node 22+ ships ``--experimental-strip-types`` (stable in 23+, default
off in 24); we spawn ``node --experimental-strip-types`` and import the
TS twin directly — no transpile step, no node_modules dependency, no
generated build artefact. The driver script is tiny + held inline as a
heredoc to keep the test self-contained.

A single Node subprocess runs all fixtures (success + negative) and
emits one JSON blob; the session-scoped fixture caches that across the
parametrized tests so the spawn cost amortises to one invocation per
``pytest`` session.

The whole family ``pytest.skip``s if Node is unavailable (or below
v22) — this matches the AS.1.2 / AS.1.3 / AS.1.4 cross-twin tests'
"skip if TS twin file is absent" gating; CI must have Node.

Module-global state audit (per implement_phase_step.md SOP §1)
──────────────────────────────────────────────────────────────
* All fixture data lives in module-level dict literals containing only
  immutable scalars / nested dicts of immutable scalars — each pytest
  worker re-imports them with byte-identical content (answer #1 of
  SOP §1: deterministic-by-construction across workers).
* The session-scoped Node-output cache lives on the pytest fixture, not
  module-level — pytest manages its lifecycle per worker.
* No DB, no network IO, no env reads at module import time.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
from typing import Any, Mapping

import pytest

from backend.security import oauth_client as oc


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Paths + Node gating
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_TS_TWIN_PATH = (
    pathlib.Path(__file__).resolve().parents[2]
    / "templates"
    / "_shared"
    / "oauth-client"
    / "index.ts"
)

# Frozen reference time for deterministic ``expires_at`` arithmetic.
# Picked outside any DST boundary, well in the future of any test data,
# so the absolute-timestamp output is stable across CI runners regardless
# of host TZ.
_NOW = 1_700_000_000


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
    # ``v24.14.1`` → strip leading ``v`` then split.
    raw = r.stdout.strip().lstrip("v")
    try:
        major = int(raw.split(".", 1)[0])
    except (ValueError, IndexError):
        return False
    # ``--experimental-strip-types`` landed in Node 22.6 and is the
    # default in 23+. We require ≥22 to be safe.
    return major >= 22


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Vendor-real fixtures (11 — one per AS.1.3 catalog entry)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Each fixture's shape mirrors the real vendor response captured from
# their developer docs as of the AS.1.3 row landing (2026-04-28). Field
# values are synthetic but the *keys* and *value types* are vendor-real.

VENDOR_FIXTURES: Mapping[str, Mapping[str, Any]] = {
    "github": {
        # GitHub OAuth — comma-separated scope, no expires_in (classic
        # OAuth Apps); GitHub Apps "expiring user-to-server" mode would
        # add expires_in + refresh_token, exercised by ``google`` below.
        "access_token": "gho_aaa111",
        "scope": "read:user,user:email",
        "token_type": "bearer",
    },
    "google": {
        # Google OIDC — full shape with refresh_token (requires
        # access_type=offline + prompt=consent at authorize time).
        "access_token": "ya29.a0b1c2",
        "expires_in": 3599,
        "refresh_token": "1//0gAaa1Bbb2",
        "scope": "openid email profile https://www.googleapis.com/auth/userinfo.email",
        "token_type": "Bearer",
        "id_token": "eyJhbGciOi.payload.sig",
    },
    "microsoft": {
        # MS Entra ID v2.0 — scope is the granted set echoed back; note
        # the trailing ``offline_access`` that drives refresh_token
        # issuance (different idiom from Google's query param).
        "access_token": "EwBwA0X2A.token",
        "expires_in": 3600,
        "refresh_token": "M.C540_BAY.0.U.-Cb1abc",
        "scope": "openid email profile offline_access",
        "token_type": "Bearer",
        "id_token": "eyJ0eXAi.ms-id-token.sig",
    },
    "apple": {
        # Sign in with Apple — id_token-centric, no scope echoed in the
        # token response (caller infers from the original authorize
        # request). Refresh_token issued only on first auth.
        "access_token": "a.aapl.access",
        "expires_in": 3600,
        "refresh_token": "r.aapl.refresh",
        "token_type": "Bearer",
        "id_token": "eyJraWQi.apple-id.sig",
    },
    "gitlab": {
        # GitLab — extra ``created_at`` surfaced via raw; OIDC mode
        # would also emit id_token (caller adds ``openid`` to scope).
        "access_token": "glpat-aaa111",
        "token_type": "Bearer",
        "expires_in": 7200,
        "refresh_token": "rtoken_bbb222",
        "scope": "read_user",
        "created_at": 1700000000,
    },
    "bitbucket": {
        # Bitbucket Cloud — ``state`` field is non-standard but echoed
        # back (the original authorize-time state); surfaced via raw.
        "access_token": "bb-access-aaa",
        "scope": "account email",
        "expires_in": 7200,
        "refresh_token": "bb-refresh-bbb",
        "state": "state_value_for_csrf",
        "token_type": "bearer",
    },
    "slack": {
        # Slack v2 — bot token at top level; nested ``authed_user``
        # carries the per-user OAuth bundle. Both nested + extras land
        # in raw verbatim.
        "ok": True,
        "app_id": "A012345",
        "authed_user": {
            "id": "U987654",
            "scope": "users:read,users:read.email",
            "access_token": "xoxp-user-aaa",
            "token_type": "user",
        },
        "scope": "users:read,users:read.email",
        "token_type": "Bearer",
        "access_token": "xoxb-bot-bbb",
        "bot_user_id": "B012345",
        "team": {"id": "T012345", "name": "Acme"},
    },
    "notion": {
        # Notion — workspace-scoped, no expires_in (token never expires),
        # no refresh_token (no refresh grant). Vendor extras land in raw.
        "access_token": "secret_aaa111",
        "token_type": "bearer",
        "bot_id": "bot_xxx",
        "workspace_name": "Acme Workspace",
        "workspace_icon": "https://example.com/icon.png",
        "workspace_id": "ws_yyy",
        "owner": {"type": "user", "user": {"id": "user_zzz"}},
    },
    "salesforce": {
        # Salesforce — many extras (instance_url is critical for caller
        # to know which org host to address); surfaced via raw.
        "access_token": "00DAA0000000aaa!ARQAQ.token",
        "refresh_token": "5Aep861aaa.refresh",
        "scope": "openid email profile",
        "instance_url": "https://acme.my.salesforce.com",
        "id": "https://login.salesforce.com/id/00D.../005...",
        "token_type": "Bearer",
        "issued_at": "1700000000000",
        "signature": "ZmFrZS1zaWc=",
    },
    "hubspot": {
        # HubSpot — minimal shape, no token_type echoed (parser default
        # to "Bearer" is what makes both sides agree).
        "access_token": "hs-access-aaa",
        "refresh_token": "hs-refresh-bbb",
        "expires_in": 1800,
    },
    "discord": {
        # Discord — standard RFC 6749 §5.1 shape.
        "access_token": "discord-access-aaa",
        "expires_in": 604800,
        "refresh_token": "discord-refresh-bbb",
        "scope": "identify email",
        "token_type": "Bearer",
    },
}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Quirk fixtures (parser-edge behaviour)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

QUIRK_FIXTURES: Mapping[str, Mapping[str, Any]] = {
    "scope_comma": {
        # Both sides must split + dedupe a comma-separated scope to
        # the same ordered tuple/array.
        "access_token": "tok",
        "scope": "read,write,read",
        "token_type": "Bearer",
    },
    "scope_list": {
        # When the vendor returns scope as a JSON array, both sides
        # must preserve order without re-splitting.
        "access_token": "tok",
        "scope": ["read", "write", "admin"],
        "token_type": "Bearer",
    },
    "scope_dedupe": {
        # Mixed comma + space + duplicate tokens — both sides must
        # produce the same dedupe order ("read", "write", "admin").
        "access_token": "tok",
        "scope": "read read,write read admin write",
        "token_type": "Bearer",
    },
    "string_expires_in": {
        # Some vendors send expires_in as a JSON string; both sides
        # MUST coerce to number identically.
        "access_token": "tok",
        "expires_in": "3600",
        "token_type": "Bearer",
    },
    "zero_expires_in": {
        # Boundary: expires_in=0 is valid (means "expired now").
        # Negative is rejected; zero is accepted by both sides.
        "access_token": "tok",
        "expires_in": 0,
        "token_type": "Bearer",
    },
}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Negative fixtures (both sides must reject)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

NEGATIVE_FIXTURES: Mapping[str, Mapping[str, Any]] = {
    "error_shape": {
        # RFC 6749 §5.2 error payload — both sides MUST raise
        # TokenResponseError.
        "error": "invalid_grant",
        "error_description": "Authorization code expired",
    },
    "missing_access": {
        # No ``access_token`` field — both MUST raise.
        "token_type": "Bearer",
        "expires_in": 3600,
    },
    "negative_expires": {
        # Negative expires_in is a vendor bug; both MUST reject rather
        # than silently store a past timestamp.
        "access_token": "tok",
        "expires_in": -1,
        "token_type": "Bearer",
    },
}


_ALL_SUCCESS_FIXTURES: Mapping[str, Mapping[str, Any]] = {
    **VENDOR_FIXTURES,
    **{f"quirk_{k}": v for k, v in QUIRK_FIXTURES.items()},
}
_ALL_NEGATIVE_FIXTURES: Mapping[str, Mapping[str, Any]] = {
    f"neg_{k}": v for k, v in NEGATIVE_FIXTURES.items()
}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Shape normalisation (Python → camelCase JSON-comparable dict)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _python_token_shape(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Run Python's parse_token_response, project to the comparable
    shape (camelCase, list-not-tuple scope, dict raw)."""
    ts = oc.parse_token_response(payload, now=_NOW)
    return {
        "accessToken": ts.access_token,
        "refreshToken": ts.refresh_token,
        "tokenType": ts.token_type,
        "expiresAt": ts.expires_at,
        "scope": list(ts.scope),
        "idToken": ts.id_token,
        "raw": dict(ts.raw),
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Node driver — held inline as a heredoc, single subprocess
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_NODE_DRIVER = """
import { parseTokenResponse } from %(twin_path)s;
import { readFileSync } from "node:fs";

const input = JSON.parse(readFileSync(0, "utf-8"));
const out = {};

for (const [k, payload] of Object.entries(input.fixtures)) {
  try {
    const value = parseTokenResponse(payload, { now: input.now });
    out[k] = { ok: true, value };
  } catch (e) {
    out[k] = {
      ok: false,
      errorName: (e && e.constructor && e.constructor.name) || "Error",
      message: String((e && e.message) || e),
    };
  }
}

process.stdout.write(JSON.stringify(out));
"""


def _run_ts_driver(fixtures: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Spawn ``node --experimental-strip-types`` to import the TS twin
    and parse every fixture in *fixtures*. Returns the JSON-decoded
    output keyed by fixture name."""

    twin_path_literal = json.dumps(str(_TS_TWIN_PATH))
    driver_src = _NODE_DRIVER % {"twin_path": twin_path_literal}

    # ``--input-type=module`` tells Node the stdin script is ESM (so
    # `import` works); ``--experimental-strip-types`` lets the imported
    # ``.ts`` file load directly without a transpile step.
    cmd = [
        "node",
        "--no-warnings",
        "--experimental-strip-types",
        "--input-type=module",
        "--eval",
        driver_src,
    ]

    payload = json.dumps({"now": _NOW, "fixtures": dict(fixtures)})
    env = dict(os.environ)
    # Pin the runtime to a non-DST-sensitive UTC so any clock-aware
    # branch in the parser (there isn't one today, but keep the seal
    # tight for future changes) is deterministic across CI hosts.
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


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pytest fixtures
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.fixture(scope="session")
def ts_parsed_shapes() -> dict[str, Any]:
    """Run the TS twin once per pytest session over every fixture
    (success + negative). Parametrized tests look up their own row;
    Node spawns exactly once."""

    if not _TS_TWIN_PATH.exists():
        pytest.skip(f"TS twin not present at {_TS_TWIN_PATH}")
    if not _node_supports_strip_types():
        pytest.skip(
            "node ≥22 with --experimental-strip-types not available; "
            "TS twin behaviour cannot be exercised"
        )

    all_fixtures = {**_ALL_SUCCESS_FIXTURES, **_ALL_NEGATIVE_FIXTURES}
    return _run_ts_driver(all_fixtures)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 1 — Per-vendor token-shape parity (11 tests)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.parametrize(
    "vendor", sorted(VENDOR_FIXTURES.keys()), ids=lambda v: v
)
def test_vendor_token_shape_parity_python_ts(
    vendor: str, ts_parsed_shapes: dict[str, Any]
) -> None:
    """For every catalog vendor, both parsers produce the same shape.

    Catches behavioural drift like a regex change in the scope splitter
    that AS.1.2's static-constant drift guard wouldn't see.
    """
    payload = VENDOR_FIXTURES[vendor]
    py_shape = _python_token_shape(payload)

    ts_row = ts_parsed_shapes[vendor]
    assert ts_row["ok"], (
        f"TS twin raised on vendor {vendor!r} fixture: {ts_row.get('message')!r}"
    )
    ts_shape = ts_row["value"]

    assert py_shape == ts_shape, (
        f"token-shape drift between Python and TS twin for vendor {vendor!r}\n"
        f"  payload:  {json.dumps(payload, sort_keys=True)}\n"
        f"  Python:   {json.dumps(py_shape, sort_keys=True)}\n"
        f"  TS twin:  {json.dumps(ts_shape, sort_keys=True)}"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 2 — Quirk-fixture parity (5 tests)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.parametrize(
    "quirk", sorted(QUIRK_FIXTURES.keys()), ids=lambda v: v
)
def test_quirk_token_shape_parity_python_ts(
    quirk: str, ts_parsed_shapes: dict[str, Any]
) -> None:
    """Edge cases the parsers historically diverged on."""
    payload = QUIRK_FIXTURES[quirk]
    py_shape = _python_token_shape(payload)

    ts_row = ts_parsed_shapes[f"quirk_{quirk}"]
    assert ts_row["ok"], (
        f"TS twin raised on quirk {quirk!r} fixture: {ts_row.get('message')!r}"
    )
    ts_shape = ts_row["value"]

    assert py_shape == ts_shape, (
        f"token-shape drift between Python and TS twin for quirk {quirk!r}\n"
        f"  payload:  {json.dumps(payload, sort_keys=True)}\n"
        f"  Python:   {json.dumps(py_shape, sort_keys=True)}\n"
        f"  TS twin:  {json.dumps(ts_shape, sort_keys=True)}"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 3 — Negative-fixture parity (3 tests)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.parametrize(
    "neg", sorted(NEGATIVE_FIXTURES.keys()), ids=lambda v: v
)
def test_negative_token_shape_parity_python_ts(
    neg: str, ts_parsed_shapes: dict[str, Any]
) -> None:
    """Both parsers MUST reject the same vendor-bug payloads.

    Message-string comparison is deliberately skipped — Python uses
    ``repr()`` for the description, TS uses ``JSON.stringify``; both
    are valid contract-internal choices, neither part of the public
    surface that callers depend on. The contract is "raise vs not".
    """
    payload = NEGATIVE_FIXTURES[neg]

    py_raised = False
    py_error_name: str | None = None
    try:
        oc.parse_token_response(payload, now=_NOW)
    except oc.TokenResponseError as e:
        py_raised = True
        py_error_name = type(e).__name__

    ts_row = ts_parsed_shapes[f"neg_{neg}"]
    ts_raised = not ts_row["ok"]
    ts_error_name = ts_row.get("errorName") if ts_raised else None

    assert py_raised, f"Python parser failed to reject negative fixture {neg!r}"
    assert ts_raised, (
        f"TS twin failed to reject negative fixture {neg!r}; "
        f"got {ts_row!r}"
    )
    assert py_error_name == ts_error_name == "TokenResponseError", (
        f"error-class drift on {neg!r}: Python={py_error_name!r} "
        f"TS={ts_error_name!r}"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 4 — Aggregate SHA-256 oracle
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _normalize_numbers(obj: Any) -> Any:
    """Recursively convert integer-valued floats to ints so the JSON
    serialisation is byte-identical across Python (``1700003600.0``)
    and JS round-trip (``1700003600``).

    Python's ``float + int`` yields ``float``; JS's number is unified.
    Both are semantically the same value (``1.0 == 1``), but
    ``json.dumps`` writes ``1.0`` for the former and ``1`` for the
    latter, so a SHA-256 over the serialised string would falsely
    report drift. This normaliser is the cheapest fix that keeps the
    hash a useful one-line oracle.
    """
    if isinstance(obj, dict):
        return {k: _normalize_numbers(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_normalize_numbers(v) for v in obj]
    if isinstance(obj, float) and obj.is_integer():
        return int(obj)
    return obj


def test_all_fixtures_aggregate_sha256_parity_python_ts(
    ts_parsed_shapes: dict[str, Any]
) -> None:
    """One hash over every success-fixture parsed shape.

    Catches the "many-tiny-drifts" failure mode that per-fixture tests
    can't summarise — if 3 different fixtures drift by 1 char each, this
    oracle yields a single short error message that's easier to triage
    than 3 long ``==`` diffs. The per-fixture tests remain the primary
    oracle for *which* vendor drifted; this is the cheap-to-report
    canary.
    """
    import hashlib

    py_blob: dict[str, Any] = {}
    ts_blob: dict[str, Any] = {}
    for key in sorted(_ALL_SUCCESS_FIXTURES.keys()):
        py_blob[key] = _python_token_shape(_ALL_SUCCESS_FIXTURES[key])
        ts_row = ts_parsed_shapes[key]
        assert ts_row["ok"], f"unexpected TS-side error on {key}: {ts_row}"
        ts_blob[key] = ts_row["value"]

    py_serialized = json.dumps(_normalize_numbers(py_blob), sort_keys=True)
    ts_serialized = json.dumps(_normalize_numbers(ts_blob), sort_keys=True)

    py_hash = hashlib.sha256(py_serialized.encode("utf-8")).hexdigest()
    ts_hash = hashlib.sha256(ts_serialized.encode("utf-8")).hexdigest()

    assert py_hash == ts_hash, (
        f"aggregate token-shape drift between Python and TS twin\n"
        f"  Python SHA-256: {py_hash}\n"
        f"  TS     SHA-256: {ts_hash}\n"
        f"  (run per-fixture tests for which vendor drifted)"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 5 — Coverage guard (catalog ↔ fixtures)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_vendor_fixture_set_covers_catalog() -> None:
    """Every catalog vendor MUST have a parity fixture in this file.

    Catches the "added a 12th vendor without a fixture" failure mode —
    AS.1.6 / AS.1.7 add new providers and the row-author forgets to
    write the matching fixture. Without this guard the new vendor would
    silently skip parity coverage.
    """
    from backend.security import oauth_vendors as ov

    catalog_ids = set(ov.ALL_VENDOR_IDS)
    fixture_ids = set(VENDOR_FIXTURES.keys())
    missing = catalog_ids - fixture_ids
    extra = fixture_ids - catalog_ids
    assert not missing, (
        f"AS.1.5 vendor fixture set is missing entries for: {sorted(missing)}"
    )
    assert not extra, (
        f"AS.1.5 vendor fixture set has stale entries not in catalog: "
        f"{sorted(extra)}"
    )
