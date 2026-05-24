"""OP-1660 — fail-closed side-effect guard for DAG-executor handlers.

Covers the acceptance criteria for the guard (design doc §7 Dim 6, codex E8):

  * Code — a guard that fail-closes OUTSIDE prod: dev/staging/unset env, a
    missing/false per-capability allow flag, or an unknown capability all
    raise :class:`SideEffectBlocked`; the *only* permitted path is
    ``OMNISIGHT_ENV=prod`` AND ``OMNISIGHT_ALLOW_<CAPABILITY>`` truthy.
  * Integration — a (fake) handler attempting Gerrit / SSH on
    ``OMNISIGHT_ENV=dev``/``staging`` raises and aborts the task BEFORE any
    network call: the fake endpoint is wired to FAIL if it is ever reached,
    and we assert it never recorded a call.
  * Exercised — a staging-env run with fake creds present does NOT push: the
    guard blocks first, so the fake "push" never runs.

MUST NOT (and does not): contact any real Gerrit / registry / SSH host /
hardware / prod DB. Every endpoint here is an in-memory fake; the env is
faked via ``env=`` overrides and ``monkeypatch.setenv``. The guard has no
test/CI carveout, so these assertions reflect real-process behaviour.
"""

from __future__ import annotations

import pytest

from backend import dag_executor as dx


ALL_CAPS = sorted(dx.SIDE_EFFECT_CAPABILITIES)
NON_PROD_ENVS = ["dev", "develop", "development", "staging", "stg", None, "", "qa"]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Fake endpoints — record (or refuse) instead of touching the network.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class _FakeEndpoint:
    """Records every "network" call. ``explode=True`` makes any call a hard
    failure, so a guard that lets one through is impossible to miss."""

    def __init__(self, name: str, *, explode: bool = False) -> None:
        self.name = name
        self.explode = explode
        self.calls: list[tuple] = []

    def __call__(self, *args):
        if self.explode:
            raise AssertionError(
                f"{self.name}: network call reached despite the guard — "
                f"args={args!r}"
            )
        self.calls.append(args)
        return f"{self.name}-ok"


async def _gerrit_push_handler(endpoint, *, env=None):
    """A stand-in for a future nonlocal handler that pushes to Gerrit.

    It wraps its (fake) push in :func:`perform_side_effect`, exactly as the
    real handler will, so the guard runs BEFORE the network call.
    """
    return dx.perform_side_effect(
        dx.GERRIT_PUSH,
        lambda: endpoint("refs/for/develop"),
        source="fake-gerrit-handler",
        env=env,
    )


async def _ssh_flash_handler(endpoint, *, env=None):
    return dx.perform_side_effect(
        dx.SSH,
        lambda: endpoint("ssh://device.local", "flash firmware.bin"),
        source="fake-ssh-handler",
        env=env,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Code: the guarded capability set is exactly the five named in the ticket
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_guarded_capabilities_are_exactly_the_five():
    assert dx.SIDE_EFFECT_CAPABILITIES == frozenset({
        "gerrit_push", "registry_publish", "ssh",
        "hardware_flash", "prod_db_write",
    })


@pytest.mark.parametrize("cap", ALL_CAPS)
def test_allow_flag_env_naming(cap):
    assert dx.allow_flag_env(cap) == "OMNISIGHT_ALLOW_" + cap.upper()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Code: fail-closed OUTSIDE prod (the core protection)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.parametrize("cap", ALL_CAPS)
@pytest.mark.parametrize("env", NON_PROD_ENVS)
def test_blocks_outside_prod_even_with_allow_flag(cap, env, monkeypatch):
    # Allow flag present (fake creds / fake intent) — must STILL block,
    # because the env is not prod.
    monkeypatch.setenv(dx.allow_flag_env(cap), "1")
    with pytest.raises(dx.SideEffectBlocked) as ei:
        dx.guard_side_effect(cap, source="t", env=env)
    assert ei.value.capability == cap
    assert "prod" in str(ei.value)


@pytest.mark.parametrize("cap", ALL_CAPS)
def test_blocks_in_prod_when_allow_flag_unset(cap, monkeypatch):
    monkeypatch.delenv(dx.allow_flag_env(cap), raising=False)
    with pytest.raises(dx.SideEffectBlocked) as ei:
        dx.guard_side_effect(cap, source="t", env="prod")
    assert dx.allow_flag_env(cap) in str(ei.value)


@pytest.mark.parametrize("cap", ALL_CAPS)
@pytest.mark.parametrize("falsey", ["0", "false", "no", "off", "", "  "])
def test_blocks_in_prod_when_allow_flag_falsey(cap, falsey, monkeypatch):
    monkeypatch.setenv(dx.allow_flag_env(cap), falsey)
    with pytest.raises(dx.SideEffectBlocked):
        dx.guard_side_effect(cap, source="t", env="prod")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Code: the ONLY permitted path — prod + truthy allow flag
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.parametrize("cap", ALL_CAPS)
@pytest.mark.parametrize("truthy", ["1", "true", "TRUE", "yes", "on"])
def test_allowed_in_prod_with_truthy_allow_flag(cap, truthy, monkeypatch):
    monkeypatch.setenv(dx.allow_flag_env(cap), truthy)
    # Permitted → returns None, does not raise.
    assert dx.guard_side_effect(cap, source="t", env="prod") is None


def test_allow_flag_is_per_capability_not_global(monkeypatch):
    # Setting ONE capability's flag must not unlock the others.
    monkeypatch.setenv(dx.allow_flag_env(dx.GERRIT_PUSH), "1")
    for other in ALL_CAPS:
        if other == dx.GERRIT_PUSH:
            continue
        monkeypatch.delenv(dx.allow_flag_env(other), raising=False)
        with pytest.raises(dx.SideEffectBlocked):
            dx.guard_side_effect(other, source="t", env="prod")
    # gerrit_push itself is permitted.
    assert dx.guard_side_effect(dx.GERRIT_PUSH, source="t", env="prod") is None


def test_reads_omnisight_env_from_environment(monkeypatch):
    # No env= override → guard reads OMNISIGHT_ENV from the process env.
    monkeypatch.setenv("OMNISIGHT_ENV", "staging")
    monkeypatch.setenv(dx.allow_flag_env(dx.GERRIT_PUSH), "1")
    with pytest.raises(dx.SideEffectBlocked):
        dx.guard_side_effect(dx.GERRIT_PUSH, source="t")

    monkeypatch.setenv("OMNISIGHT_ENV", "prod")
    assert dx.guard_side_effect(dx.GERRIT_PUSH, source="t") is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Code: unknown capability → fail-closed (never silently allowed)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_unknown_capability_is_blocked_even_in_prod(monkeypatch):
    monkeypatch.setenv("OMNISIGHT_ALLOW_DELETE_PROD", "1")
    with pytest.raises(dx.SideEffectBlocked) as ei:
        dx.guard_side_effect("delete_prod", source="t", env="prod")
    assert ei.value.capability == "delete_prod"
    assert "unknown" in str(ei.value).lower()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  perform_side_effect: action runs ONLY after the guard passes
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_perform_side_effect_skips_action_when_blocked():
    calls: list[int] = []

    def action():
        calls.append(1)
        return "done"

    with pytest.raises(dx.SideEffectBlocked):
        dx.perform_side_effect(
            dx.REGISTRY_PUBLISH, action, source="t", env="dev",
        )
    assert calls == []  # action never ran — aborted before the side effect


def test_perform_side_effect_runs_action_when_allowed(monkeypatch):
    monkeypatch.setenv(dx.allow_flag_env(dx.REGISTRY_PUBLISH), "1")
    out = dx.perform_side_effect(
        dx.REGISTRY_PUBLISH, lambda: "published", source="t", env="prod",
    )
    assert out == "published"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Integration: handler attempting Gerrit/SSH on dev/staging raises +
#  aborts BEFORE any network call (fake endpoints that EXPLODE if reached)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.parametrize("env", ["dev", "staging"])
async def test_gerrit_handler_aborts_before_network_on_non_prod(env):
    endpoint = _FakeEndpoint("gerrit", explode=True)
    with pytest.raises(dx.SideEffectBlocked) as ei:
        await _gerrit_push_handler(endpoint, env=env)
    assert ei.value.capability == dx.GERRIT_PUSH
    assert endpoint.calls == []  # never reached the (fake) push


@pytest.mark.parametrize("env", ["dev", "staging"])
async def test_ssh_handler_aborts_before_network_on_non_prod(env):
    endpoint = _FakeEndpoint("ssh", explode=True)
    with pytest.raises(dx.SideEffectBlocked) as ei:
        await _ssh_flash_handler(endpoint, env=env)
    assert ei.value.capability == dx.SSH
    assert endpoint.calls == []


async def test_gerrit_handler_pushes_only_in_prod_with_flag(monkeypatch):
    monkeypatch.setenv(dx.allow_flag_env(dx.GERRIT_PUSH), "1")
    endpoint = _FakeEndpoint("gerrit")  # records, does not explode
    out = await _gerrit_push_handler(endpoint, env="prod")
    assert out == "gerrit-ok"
    assert endpoint.calls == [("refs/for/develop",)]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Exercised: a staging run with fake creds present does NOT push
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def test_staging_run_with_fake_creds_does_not_push(monkeypatch):
    # Simulate a staging host that HAS the allow flag set (fake creds /
    # leftover config). The guard must still block: env != prod.
    monkeypatch.setenv("OMNISIGHT_ENV", "staging")
    monkeypatch.setenv(dx.allow_flag_env(dx.GERRIT_PUSH), "1")
    monkeypatch.setenv("GERRIT_HTTP_PASSWORD", "fake-creds-present")  # noqa: S105

    endpoint = _FakeEndpoint("gerrit", explode=True)
    with pytest.raises(dx.SideEffectBlocked):
        await _gerrit_push_handler(endpoint)  # reads OMNISIGHT_ENV=staging
    assert endpoint.calls == []  # the guard blocked before any push
