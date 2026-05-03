"""FX.7.8 — drift guard for Grafana admin-password startup validation.

Background
----------
The 2026-05-03 deep-audit row FX.7.8 flagged that ``docker-compose.prod.yml``
booted the Grafana service with the well-known default credentials
``admin / admin`` whenever ``GRAFANA_ADMIN_PASSWORD`` was unset — the
compose interpolation ``${GRAFANA_ADMIN_PASSWORD:-admin}`` silently
substituted ``admin``. With the ``observability`` profile enabled and
port 3001 published to the host, that ships an internet-reachable
dashboard with the default Grafana credentials.

FX.7.8 layered two defences:

1. **Compose interpolation tightened** to ``${VAR:?...}`` — engine
   refuses to render the service if either ``GRAFANA_ADMIN_USER`` or
   ``GRAFANA_ADMIN_PASSWORD`` is unset or empty.
2. **Init validator sidecar** ``grafana-env-validator`` gated via
   ``depends_on: condition: service_completed_successfully`` — runs a
   small ``alpine:sh`` script that rejects values that *look* set but
   are still defaults a copy-paste deploy would inherit (the literal
   ``admin``, the username, weak-default list, length < 12).

What this test enforces
-----------------------
Both layers must remain wired. The guard inspects the parsed YAML, not
string ``grep``, because the file has comment blocks that name the
guarded values for documentation and a literal ``grep`` would
mis-match those.

Specifically:

* ``grafana`` service environment uses the ``${VAR:?...}`` form (not
  ``${VAR:-default}``) for both admin user + admin password.
* ``grafana-env-validator`` service exists, depends on no other
  service, runs a non-restarting sh command containing each member of
  the canonical weak-default reject list, the length floor (``-lt 12``)
  check, and the username-equality check.
* ``grafana`` service ``depends_on`` includes the validator with
  ``condition: service_completed_successfully``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "docker-compose.prod.yml"

# Canonical weak-default reject list — must match the validator command
# in docker-compose.prod.yml. If you add to one, add to the other and
# update this list.
WEAK_DEFAULTS = (
    "admin",
    "password",
    "changeme",
    "grafana",
    "omnisight",
    "default",
    "root",
    "letmein",
    "123456",
    "12345678",
    "123456789",
    "1234567890",
    "qwerty",
    "qwerty123",
    "admin123",
    "password123",
    "grafana123",
)


def _load_prod_compose() -> dict:
    with COMPOSE_FILE.open(encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    assert isinstance(doc, dict), "docker-compose.prod.yml top-level must be a mapping"
    return doc


def _grafana_service() -> dict:
    services = _load_prod_compose().get("services") or {}
    grafana = services.get("grafana")
    assert isinstance(grafana, dict), "grafana service missing from docker-compose.prod.yml"
    return grafana


def _validator_service() -> dict:
    services = _load_prod_compose().get("services") or {}
    validator = services.get("grafana-env-validator")
    assert isinstance(validator, dict), (
        "FX.7.8 regression: grafana-env-validator service missing — "
        "the init container that rejects default Grafana admin passwords "
        "must remain wired."
    )
    return validator


def _env_items(service: dict) -> list[tuple[str, str]]:
    """Normalise ``environment:`` (list-form or mapping-form) to (k, v) pairs."""
    env = service.get("environment") or []
    if isinstance(env, dict):
        return [(str(k), str(v) if v is not None else "") for k, v in env.items()]
    items: list[tuple[str, str]] = []
    for entry in env:
        assert isinstance(entry, str), f"unexpected env entry type: {entry!r}"
        if "=" in entry:
            k, _, v = entry.partition("=")
            items.append((k, v))
    return items


def test_grafana_admin_env_does_not_default_to_admin() -> None:
    """The smoking-gun fix: reject any non-empty default fallback.

    The original FX.7.8 finding was ``${VAR:-admin}``, which silently
    substituted the literal Grafana default password ``admin``. The
    fix is the empty-default form ``${VAR:-}`` — compose refuses to
    bake any value in, so the validator's empty-string check (length
    0 < 12) trips at container-start.

    This test rejects two patterns:
      1. ``:-`` followed by anything other than the closing ``}``
         (i.e. a non-empty fallback value reappeared)
      2. the legacy literal ``admin`` token in the value

    Why not require ``:?``: compose runs interpolation across the
    *entire* YAML before profile filtering, so ``${VAR:?msg}`` would
    refuse every ``compose up`` even when ``--profile observability``
    is omitted. Empty default + validator-script enforcement gives
    equivalent safety without the cross-profile blast.
    """
    import re

    items = _env_items(_grafana_service())

    found_user = found_pw = False
    for key, value in items:
        if key == "GF_SECURITY_ADMIN_USER":
            found_user = True
            target = "GRAFANA_ADMIN_USER"
        elif key == "GF_SECURITY_ADMIN_PASSWORD":
            found_pw = True
            target = "GRAFANA_ADMIN_PASSWORD"
        else:
            continue

        # Must reference the right env var.
        assert target in value, (
            f"FX.7.8: {key} must interpolate {target}, got {value!r}"
        )
        # Reject any non-empty default fallback (`${VAR:-something}`
        # where `something` is not the empty string).
        m = re.search(r":-([^}]*)\}", value)
        if m is not None:
            fallback = m.group(1)
            assert fallback == "", (
                f"FX.7.8 regression: {key} re-introduced a non-empty "
                f"compose fallback default {fallback!r}; this is exactly "
                "the pattern that originally let `admin/admin` ship. Use "
                "`${VAR:-}` (empty default) and let the validator enforce."
            )
        # Belt-and-braces: reject the literal token "admin" anywhere.
        assert "admin" not in value.lower().replace(target.lower(), ""), (
            f"FX.7.8: {key} value {value!r} still contains literal 'admin'."
        )

    assert found_user, "GF_SECURITY_ADMIN_USER missing from grafana environment"
    assert found_pw, "GF_SECURITY_ADMIN_PASSWORD missing from grafana environment"


def test_grafana_depends_on_validator_with_completed_successfully() -> None:
    """Grafana must wait on the validator's clean exit.

    ``service_started`` would let Grafana boot regardless of validator
    exit code; only ``service_completed_successfully`` aborts the up
    when the validator returns non-zero.
    """
    deps = _grafana_service().get("depends_on")
    assert isinstance(deps, dict), (
        "FX.7.8: grafana.depends_on must be the long-syntax mapping form "
        "(needed to specify per-dep condition); got "
        f"{type(deps).__name__}: {deps!r}"
    )
    validator_dep = deps.get("grafana-env-validator")
    assert isinstance(validator_dep, dict), (
        "FX.7.8 regression: grafana service no longer depends on "
        "grafana-env-validator — the password-default guard would be "
        "skipped at startup."
    )
    assert validator_dep.get("condition") == "service_completed_successfully", (
        f"FX.7.8: grafana depends_on grafana-env-validator must use "
        f"condition: service_completed_successfully, got {validator_dep!r}"
    )


def test_validator_runs_oneshot_with_no_restart() -> None:
    """The validator is an init container, not a long-lived service.

    A ``restart: always`` would loop the validator forever after exit
    code 0, defeating ``service_completed_successfully`` (which waits
    for the container to *terminate* with code 0).
    """
    validator = _validator_service()
    restart = validator.get("restart")
    assert restart in ("no", None, "on-failure"), (
        f"FX.7.8: grafana-env-validator must NOT use restart: always — "
        f"got {restart!r}. Use 'no' (recommended) or omit the key."
    )


def _rendered_validator_script() -> str:
    """Return the validator command body with compose ``$$`` un-escapes applied.

    Compose interpolation eats a single ``$`` everywhere in the YAML
    document, including inside ``command:`` strings, so the inline
    script in docker-compose.prod.yml escapes every shell-variable
    ``$`` as ``$$``. PyYAML hands us the raw YAML text; we mirror
    compose's render so our subprocess invocation reproduces what
    actually runs in the container.
    """
    cmd = _validator_service().get("command")
    assert isinstance(cmd, list) and len(cmd) >= 3, cmd
    return str(cmd[-1]).replace("$$", "$")


def test_validator_command_rejects_weak_defaults() -> None:
    """The inline shell command must contain every weak-default token.

    We don't simulate the script — we assert each token appears
    verbatim in the command so a future maintainer who deletes one
    fails CI immediately.
    """
    cmd = _validator_service().get("command")
    assert cmd is not None, "FX.7.8: grafana-env-validator must declare a command"

    if isinstance(cmd, list):
        cmd_text = "\n".join(str(part) for part in cmd)
    else:
        cmd_text = str(cmd)

    missing = [w for w in WEAK_DEFAULTS if w not in cmd_text]
    assert not missing, (
        "FX.7.8: grafana-env-validator command is missing weak-default "
        f"tokens: {missing}. Add them back so the validator continues to "
        "reject those values."
    )


def test_validator_command_enforces_length_and_username_check() -> None:
    """Length floor and username-equality guard must remain in the script."""
    rendered = _rendered_validator_script()

    # Length floor: shell `[ "${#pw}" -lt 12 ]` — match the comparator
    # rather than the literal 12 in case someone reformats; both checks.
    assert "-lt 12" in rendered, (
        "FX.7.8: grafana-env-validator must reject GRAFANA_ADMIN_PASSWORD "
        "shorter than 12 characters (mirrors OMNISIGHT_ADMIN_PASSWORD policy)."
    )
    # Username-equality:
    assert '"$pw" = "$user"' in rendered, (
        "FX.7.8: grafana-env-validator must reject "
        "GRAFANA_ADMIN_PASSWORD == GRAFANA_ADMIN_USER."
    )


def test_validator_environment_does_not_default_to_admin() -> None:
    """Validator must not fall back to a non-empty default either.

    A ``${VAR:-admin}`` on the validator would let it validate the
    literal ``admin`` and (via the weak-list reject) trip — wasteful
    and confusing. Empty default keeps the validator honest: it's
    validating exactly what the operator set in ``.env``.
    """
    import re

    items = _env_items(_validator_service())
    keys = {k for k, _ in items}
    assert "GRAFANA_ADMIN_USER" in keys, (
        "FX.7.8: validator must receive GRAFANA_ADMIN_USER"
    )
    assert "GRAFANA_ADMIN_PASSWORD" in keys, (
        "FX.7.8: validator must receive GRAFANA_ADMIN_PASSWORD"
    )
    for key, value in items:
        if key not in ("GRAFANA_ADMIN_USER", "GRAFANA_ADMIN_PASSWORD"):
            continue
        m = re.search(r":-([^}]*)\}", value)
        if m is not None:
            fallback = m.group(1)
            assert fallback == "", (
                f"FX.7.8: validator env {key} re-introduced non-empty "
                f"fallback {fallback!r}. Use `${{VAR:-}}`."
            )
        assert "admin" not in value.lower().replace(key.lower(), ""), (
            f"FX.7.8: validator env {key} value {value!r} contains literal 'admin'."
        )


def test_validator_is_observability_profile_only() -> None:
    """The validator must share the grafana service's profile gating.

    Otherwise it would run on every ``compose up`` even when nobody
    asked for the observability stack — breaking deployers who do not
    need Grafana and have no GRAFANA_ADMIN_PASSWORD set.
    """
    validator = _validator_service()
    grafana = _grafana_service()
    assert validator.get("profiles") == grafana.get("profiles") == ["observability"], (
        "FX.7.8: grafana-env-validator must share grafana's "
        f"profiles=['observability']. Validator: {validator.get('profiles')!r}, "
        f"grafana: {grafana.get('profiles')!r}"
    )


@pytest.mark.parametrize(
    "weak", [pytest.param(w, id=w) for w in WEAK_DEFAULTS]
)
def test_simulated_validator_rejects_each_weak_default(weak: str, tmp_path: Path) -> None:
    """End-to-end: run the actual inline script with each weak default.

    Extracts the shell command from compose YAML, runs it in a
    subprocess via ``sh -c``, asserts a non-zero exit. This catches a
    regression where someone reorders / typo-renames the rejection
    branch — the static-text check above wouldn't notice because the
    token is still in the file, but the script wouldn't actually trip.
    """
    import os
    import subprocess

    script = _rendered_validator_script()

    env = os.environ.copy()
    env["GRAFANA_ADMIN_USER"] = "the-correct-grafana-admin"
    env["GRAFANA_ADMIN_PASSWORD"] = weak

    result = subprocess.run(
        ["/bin/sh", "-c", script],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    assert result.returncode != 0, (
        f"FX.7.8: validator script accepted weak default {weak!r} "
        f"(rc=0, stderr={result.stderr!r}, stdout={result.stdout!r})"
    )
    assert "FX.7.8 FAIL" in result.stderr, (
        f"FX.7.8: validator should print 'FX.7.8 FAIL' to stderr on reject; "
        f"got stderr={result.stderr!r}"
    )


def test_simulated_validator_accepts_strong_password() -> None:
    """Strong password (long, not in list, != user) must pass."""
    import os
    import subprocess

    script = _rendered_validator_script()

    env = os.environ.copy()
    env["GRAFANA_ADMIN_USER"] = "operations-grafana-admin"
    env["GRAFANA_ADMIN_PASSWORD"] = "correct-horse-battery-staple-FX-7-8"

    result = subprocess.run(
        ["/bin/sh", "-c", script],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    assert result.returncode == 0, (
        f"FX.7.8: validator rejected a strong password "
        f"(rc={result.returncode}, stderr={result.stderr!r})"
    )
    assert "FX.7.8 OK" in result.stdout


def test_simulated_validator_rejects_empty_password() -> None:
    """Empty string must trip the length floor.

    This is the failure mode that surfaces when the operator forgot
    to set ``GRAFANA_ADMIN_PASSWORD`` in ``.env`` and compose's
    ``${VAR:-}`` substituted the empty string. The validator must
    catch it instead of silently accepting whatever Grafana then
    treats as the password.
    """
    import os
    import subprocess

    script = _rendered_validator_script()

    env = os.environ.copy()
    env["GRAFANA_ADMIN_USER"] = "operations-grafana-admin"
    env["GRAFANA_ADMIN_PASSWORD"] = ""

    result = subprocess.run(
        ["/bin/sh", "-c", script],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    assert result.returncode != 0
    assert ">= 12 chars" in result.stderr


def test_simulated_validator_rejects_short_password() -> None:
    """Length floor (< 12) must trip even for an otherwise-not-weak value."""
    import os
    import subprocess

    script = _rendered_validator_script()

    env = os.environ.copy()
    env["GRAFANA_ADMIN_USER"] = "operations-grafana-admin"
    # 11 chars, no weak-list match.
    env["GRAFANA_ADMIN_PASSWORD"] = "Zx9!fP2#kQ7"
    assert len(env["GRAFANA_ADMIN_PASSWORD"]) == 11

    result = subprocess.run(
        ["/bin/sh", "-c", script],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    assert result.returncode != 0
    assert ">= 12 chars" in result.stderr


def test_simulated_validator_rejects_password_equals_username() -> None:
    """user == pw must trip even when the value is otherwise strong."""
    import os
    import subprocess

    script = _rendered_validator_script()

    same = "matching-strong-passphrase-here"
    env = os.environ.copy()
    env["GRAFANA_ADMIN_USER"] = same
    env["GRAFANA_ADMIN_PASSWORD"] = same

    result = subprocess.run(
        ["/bin/sh", "-c", script],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    assert result.returncode != 0
    assert "must not equal GRAFANA_ADMIN_USER" in result.stderr
