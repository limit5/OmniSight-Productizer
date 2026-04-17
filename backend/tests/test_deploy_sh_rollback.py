"""G3 #4 — `scripts/deploy.sh --rollback` contract (TODO row 1356).

TODO row 1356:
    Rollback 腳本：`deploy.sh --rollback`（秒級切回 previous color）

The ceremony established by row 1355 (test_bluegreen_precut_ceremony.py)
deliberately keeps the OLD color's container warm for 24 h so rollback
is a single rename(2) on the `active_upstream.caddy` symlink — seconds,
not minutes. Row 1356 wires the operator-facing command around that
primitive.

Invariants this file pins (break one → break the product promise):

    (1) `--rollback` is parsed in the flag loop BEFORE positional
        resolution, so `scripts/deploy.sh --rollback` (no env arg)
        works at 3am without requiring muscle-memory to type `prod`.
    (2) Rollback short-circuits BEFORE git fetch / pip install /
        pnpm build / docker compose up / systemctl restart — otherwise
        it would take minutes, not seconds.
    (3) Fail-closed gates (in order):
          * primitive missing   → exit 5
          * no previous_color   → exit 2
          * retention expired   → exit 8 (24 h window from row 1355)
          * /readyz dead        → exit 3 (don't flip to a dead upstream)
    (4) Atomic cutover delegates to `bluegreen_switch.sh rollback`
        (the only primitive that guarantees rename(2) atomicity on
        the symlink flip — see G3 #2).
    (5) Audit breadcrumb `rollback_timestamp` is written atomically
        via `.tmp.$$` + mv so a concurrent reader (row 1357 runbook /
        timeline reconstructor) never sees a half-written file.
    (6) `OMNISIGHT_BLUEGREEN_DRY_RUN=1` and `OMNISIGHT_ROLLBACK_FORCE=1`
        / `OMNISIGHT_ROLLBACK_SKIP_PREFLIGHT=1` escape hatches exist
        for operators and contract tests. Each expired-gate bypass
        prints a DANGEROUS-level warning so nobody sets them in CI
        config and forgets.

The runtime tests exercise real bash invocations of deploy.sh against
a sandboxed state dir (OMNISIGHT_BLUEGREEN_DIR) — this is the same
sandboxing pattern test_bluegreen_atomic_switch.py uses. We do NOT
touch docker; the --rollback fast path doesn't call compose at all
(that's the whole point of the contract).

Siblings:
    * test_deploy_sh_blue_green_flag.py   — G3 #1 flag contract (24)
    * test_bluegreen_atomic_switch.py     — G3 #2 atomic primitive (32)
    * test_bluegreen_precut_ceremony.py   — G3 #3 row 1355 ceremony (29)
    * test_deploy_sh_rolling.py           — G2 #3 rolling contract (31)
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_SH = PROJECT_ROOT / "scripts" / "deploy.sh"
BLUEGREEN_SWITCH = PROJECT_ROOT / "scripts" / "bluegreen_switch.sh"
STATE_DIR_REPO = PROJECT_ROOT / "deploy" / "blue-green"


@pytest.fixture(scope="module")
def deploy_sh_text() -> str:
    assert DEPLOY_SH.exists(), f"deploy.sh missing at {DEPLOY_SH}"
    return DEPLOY_SH.read_text(encoding="utf-8")


def _rollback_body(text: str) -> str:
    """Return the body of the `if "$ROLLBACK_FLAG" == "1"` block.

    The block starts right after flag parsing and ends at `fi` before
    the ENV requirement check. We key on executable tokens so a
    surrounding comment shuffle doesn't break the test.
    """
    match = re.search(
        r'if\s+\[\[\s+"\$ROLLBACK_FLAG"\s*==\s*"1"\s*\]\];\s*then(.+?)\nfi\n',
        text,
        flags=re.DOTALL,
    )
    assert match, "could not locate `--rollback` dispatch body in deploy.sh"
    return match.group(1)


def _strip_bash_comments(body: str) -> str:
    """Return the body with `#` comment lines stripped.

    Used by the no-build contract so a COMMENT mentioning
    `docker compose up` (e.g. an operator bypass hint) isn't mistaken
    for an actual invocation. We drop only full-line comments — inline
    comments after a real command are rare in this script and the
    rollback block has none today.
    """
    lines: list[str] = []
    for ln in body.splitlines():
        stripped = ln.lstrip()
        if stripped.startswith("#"):
            continue
        lines.append(ln)
    return "\n".join(lines)


def _sandbox_state(tmp_path: Path, active: str = "green", previous: str | None = "blue",
                   retention_offset_seconds: int | None = 3600) -> Path:
    """Build a blue-green state sandbox for end-to-end rollback smoke tests.

    - Copies the real upstream snippets into tmp so bluegreen_switch.sh
      can resolve target basenames.
    - Writes active_color + (optionally) previous_color + retention.
    - Points active_upstream.caddy symlink at the `active` color.
    """
    sandbox = tmp_path / "blue-green"
    sandbox.mkdir()
    for name in ("upstream-blue.caddy", "upstream-green.caddy"):
        shutil.copy2(STATE_DIR_REPO / name, sandbox / name)
    (sandbox / "active_color").write_text(active + "\n", encoding="utf-8")
    if previous is not None:
        (sandbox / "previous_color").write_text(previous + "\n", encoding="utf-8")
    if retention_offset_seconds is not None:
        retention_until = int(time.time()) + retention_offset_seconds
        (sandbox / "previous_retention_until").write_text(
            f"{retention_until}\n", encoding="utf-8"
        )
    (sandbox / "active_upstream.caddy").symlink_to(f"upstream-{active}.caddy")
    return sandbox


def _run_deploy(
    *args: str,
    env: dict[str, str] | None = None,
    timeout: int = 30,
) -> subprocess.CompletedProcess:
    base_env = os.environ.copy()
    # Rollback path doesn't hit the N10 blue-green gate (it runs before
    # the env check), but we set this for belt-and-suspenders.
    base_env.setdefault("OMNISIGHT_CHECK_BLUEGREEN", "0")
    if env:
        base_env.update(env)
    return subprocess.run(
        ["bash", str(DEPLOY_SH), *args],
        capture_output=True,
        text=True,
        env=base_env,
        cwd=str(PROJECT_ROOT),
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# (1) Flag parsing — --rollback is a first-class flag
# ---------------------------------------------------------------------------


class TestRollbackFlagParsing:
    def test_rollback_flag_declared(self, deploy_sh_text: str) -> None:
        # The flag parser must recognise `--rollback`; otherwise it
        # would fall into the `--*` unknown-flag arm and exit 1 with
        # "unknown flag". Pin the case branch textually.
        assert re.search(r'--rollback\)\s*\n\s*ROLLBACK_FLAG=1', deploy_sh_text), (
            "flag parser must have a dedicated `--rollback)` case setting "
            "ROLLBACK_FLAG=1"
        )

    def test_rollback_flag_in_usage(self, deploy_sh_text: str) -> None:
        # The usage string (printed on unknown-flag error) must advertise
        # `--rollback` so operators can discover it from a typo.
        assert "[--rollback]" in deploy_sh_text or "--rollback" in deploy_sh_text, (
            "usage string must mention `--rollback`"
        )

    def test_rollback_honored_without_env_positional(self) -> None:
        # Contract: `scripts/deploy.sh --rollback` with no env arg
        # MUST NOT fail the ENV requirement check. It should short-
        # circuit into the rollback block first. We use a bogus state
        # dir so the rollback block exits on the primitive-missing
        # guard (exit 5) rather than on the ENV validator (exit 1).
        result = _run_deploy(
            "--rollback",
            env={"OMNISIGHT_BLUEGREEN_DIR": "/nonexistent/bluegreen/state/dir"},
        )
        assert result.returncode == 5, (
            f"--rollback alone must dispatch before ENV validation "
            f"(expected exit 5 for missing state dir, got {result.returncode}).\n"
            f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
        )
        # If rollback ran, the ENV validator message must NOT appear.
        assert "env must be" not in result.stderr, (
            "--rollback must short-circuit BEFORE the ENV validator"
        )


# ---------------------------------------------------------------------------
# (2) Rollback block structure
# ---------------------------------------------------------------------------


class TestRollbackBlockStructure:
    def test_block_exists(self, deploy_sh_text: str) -> None:
        assert re.search(
            r'if\s+\[\[\s+"\$ROLLBACK_FLAG"\s*==\s*"1"\s*\]\]', deploy_sh_text
        ), "deploy.sh must have a dedicated --rollback dispatch block"

    def test_block_runs_before_env_validation(self, deploy_sh_text: str) -> None:
        # The rollback block MUST appear in the script text before the
        # `env must be 'staging' or 'prod'` validator — otherwise
        # `scripts/deploy.sh --rollback` (no env) would fail the env
        # check before ever reaching the rollback logic.
        rb_idx = deploy_sh_text.find('"$ROLLBACK_FLAG" == "1"')
        env_idx = deploy_sh_text.find("env must be 'staging' or 'prod'")
        assert rb_idx >= 0 and env_idx >= 0
        assert rb_idx < env_idx, (
            "--rollback dispatch must precede ENV validation so "
            "operators don't need to retype the env at 3am"
        )

    def test_block_runs_before_git_checkout(self, deploy_sh_text: str) -> None:
        # Rollback must short-circuit BEFORE `git fetch` / `git checkout`
        # — we don't want a rollback to tangle the working tree.
        rb_idx = deploy_sh_text.find('"$ROLLBACK_FLAG" == "1"')
        git_idx = deploy_sh_text.find("git fetch --tags")
        assert rb_idx >= 0 and git_idx >= 0
        assert rb_idx < git_idx

    def test_block_runs_before_pip_install(self, deploy_sh_text: str) -> None:
        # Rollback must run BEFORE `pip install`. The whole point is
        # seconds-level cutover — pip install takes minutes.
        rb_idx = deploy_sh_text.find('"$ROLLBACK_FLAG" == "1"')
        pip_idx = deploy_sh_text.find("pip install --quiet --require-hashes")
        assert rb_idx >= 0 and pip_idx >= 0
        assert rb_idx < pip_idx

    def test_block_runs_before_pnpm_build(self, deploy_sh_text: str) -> None:
        rb_idx = deploy_sh_text.find('"$ROLLBACK_FLAG" == "1"')
        pnpm_idx = deploy_sh_text.find("pnpm install --frozen-lockfile")
        assert rb_idx >= 0 and pnpm_idx >= 0
        assert rb_idx < pnpm_idx

    def test_block_exits_on_completion(self, deploy_sh_text: str) -> None:
        # Rollback block must `exit 0` on success — otherwise the
        # script would fall through into git checkout / pip / pnpm /
        # strategy dispatch, defeating the whole fast-path.
        body = _rollback_body(deploy_sh_text)
        assert re.search(r"\bexit\s+0\b", body), (
            "rollback block must `exit 0` on success so it doesn't "
            "fall through into the full deploy ceremony"
        )


# ---------------------------------------------------------------------------
# (3) No-build contract — rollback must NOT call build/deploy primitives
# ---------------------------------------------------------------------------


class TestNoBuildContract:
    """The rollback fast-path must NOT invoke any command that would
    make rollback take longer than seconds. Pin the absences so a
    refactor that "helpfully" re-runs pip in the rollback path is
    caught by the tests."""

    def test_no_docker_compose_up(self, deploy_sh_text: str) -> None:
        # Strip comments so an operator-facing hint mentioning
        # `docker compose up` (e.g. in a bypass warning) isn't mistaken
        # for an actual invocation.
        body = _strip_bash_comments(_rollback_body(deploy_sh_text))
        assert not re.search(
            r"docker\s+compose\s+(-f[^\n]*)?\s*up\b", body
        ), (
            "rollback block must NOT call `docker compose up` — the "
            "previous color's container is already warm (that's the "
            "whole 24 h retention point)"
        )

    def test_no_docker_compose_stop(self, deploy_sh_text: str) -> None:
        body = _strip_bash_comments(_rollback_body(deploy_sh_text))
        assert not re.search(r"docker\s+compose\s+(-f[^\n]*)?\s*stop\b", body), (
            "rollback block must NOT call `docker compose stop` — the "
            "old color becomes the new standby and stays warm for the "
            "NEXT rollback"
        )

    def test_no_pip_install(self, deploy_sh_text: str) -> None:
        body = _strip_bash_comments(_rollback_body(deploy_sh_text))
        assert "pip install" not in body, (
            "rollback must NOT `pip install` — the previous color's "
            "container already has the previous dependencies"
        )

    def test_no_pnpm_build(self, deploy_sh_text: str) -> None:
        body = _strip_bash_comments(_rollback_body(deploy_sh_text))
        assert not re.search(r"\bpnpm\s+(install|run|build)", body), (
            "rollback must NOT invoke pnpm install/run/build — the "
            "frontend bundle served by the previous color is already "
            "in its image"
        )

    def test_no_systemctl_restart(self, deploy_sh_text: str) -> None:
        body = _strip_bash_comments(_rollback_body(deploy_sh_text))
        assert "systemctl" not in body, (
            "rollback runs in the compose/Caddy topology, not systemd; "
            "systemctl calls would target the wrong lifecycle manager"
        )

    def test_no_git_fetch_or_checkout(self, deploy_sh_text: str) -> None:
        body = _strip_bash_comments(_rollback_body(deploy_sh_text))
        # `git log`/status would be harmless but `git fetch` / `git
        # checkout` would mutate the working tree during a rollback.
        assert not re.search(r"git\s+fetch", body)
        assert not re.search(r"git\s+checkout", body)


# ---------------------------------------------------------------------------
# (4) Fail-closed gates — exit codes pinned
# ---------------------------------------------------------------------------


class TestFailClosedGates:
    def test_primitive_missing_exits_5(self, deploy_sh_text: str) -> None:
        body = _rollback_body(deploy_sh_text)
        # BLUEGREEN_SWITCH + BLUEGREEN_STATE_DIR existence check must
        # exit 5 (same code the row-1355 ceremony uses for the identical
        # failure mode → uniform runbook triage).
        assert re.search(r"BLUEGREEN_SWITCH", body)
        assert re.search(r"BLUEGREEN_STATE_DIR", body)
        assert re.search(r"\bexit\s+5\b", body), (
            "rollback must `exit 5` when the blue-green primitive is "
            "missing (matches row 1355 exit semantics)"
        )

    def test_no_previous_color_exits_2(self, deploy_sh_text: str) -> None:
        body = _rollback_body(deploy_sh_text)
        assert "previous_color" in body, (
            "rollback must read deploy/blue-green/previous_color"
        )
        assert re.search(r"\bexit\s+2\b", body), (
            "rollback must `exit 2` when previous_color is missing"
        )

    def test_retention_expired_exits_8(self, deploy_sh_text: str) -> None:
        body = _rollback_body(deploy_sh_text)
        assert "previous_retention_until" in body, (
            "rollback must check deploy/blue-green/previous_retention_until"
        )
        assert re.search(r"\bexit\s+8\b", body), (
            "rollback must `exit 8` when the 24 h retention window "
            "(row 1355 breadcrumb) has expired — a DISTINCT code so "
            "the runbook can surface 'old color may be pruned, bring "
            "it back first' without guessing"
        )

    def test_readyz_dead_exits_3(self, deploy_sh_text: str) -> None:
        body = _rollback_body(deploy_sh_text)
        assert "/readyz" in body, (
            "rollback must /readyz-probe the previous color before "
            "flipping the symlink (don't flip to a dead upstream)"
        )
        assert re.search(r"\bexit\s+3\b", body), (
            "rollback must `exit 3` when previous color's /readyz is "
            "dead (matches row 1355 standby-readiness exit code)"
        )


# ---------------------------------------------------------------------------
# (5) Atomic cutover delegates to bluegreen_switch.sh
# ---------------------------------------------------------------------------


class TestAtomicCutoverDelegation:
    def test_invokes_bluegreen_switch_rollback(self, deploy_sh_text: str) -> None:
        body = _rollback_body(deploy_sh_text)
        # The symlink flip MUST go through bluegreen_switch.sh rollback
        # — it's the only primitive that does rename(2)-based atomic
        # symlink swap + writes `previous_color` + updates `active_color`
        # in the documented crash-consistent order. Inline `ln -sfn`
        # would reintroduce the two-syscall atomicity gap G3 #2 fixed.
        assert re.search(
            r'"\$BLUEGREEN_SWITCH"\s+rollback', body
        ), (
            "atomic cutover must invoke `bluegreen_switch.sh rollback` "
            "— no other primitive guarantees rename(2) atomicity"
        )

    def test_cutover_runs_after_gates(self, deploy_sh_text: str) -> None:
        body = _rollback_body(deploy_sh_text)
        # Order: gates (exit 2/3/5/8) → atomic flip → audit breadcrumb.
        # A reorder would turn the gates into vanity checks.
        rollback_invocation_idx = body.find('"$BLUEGREEN_SWITCH" rollback')
        exit_5_idx = body.find("exit 5")
        exit_2_idx = body.find("exit 2")
        assert rollback_invocation_idx >= 0
        assert 0 <= exit_5_idx < rollback_invocation_idx, (
            "exit 5 (primitive missing) must be checked before the "
            "rollback invocation"
        )
        assert 0 <= exit_2_idx < rollback_invocation_idx, (
            "exit 2 (no previous color) must be checked before the "
            "rollback invocation"
        )


# ---------------------------------------------------------------------------
# (6) Audit breadcrumb — `rollback_timestamp` atomic write
# ---------------------------------------------------------------------------


class TestAuditBreadcrumb:
    def test_rollback_timestamp_written(self, deploy_sh_text: str) -> None:
        body = _rollback_body(deploy_sh_text)
        assert "rollback_timestamp" in body, (
            "rollback must write deploy/blue-green/rollback_timestamp — "
            "row 1357 runbook + future timeline reconstructor both "
            "read it"
        )

    def test_rollback_timestamp_written_atomically(self, deploy_sh_text: str) -> None:
        body = _rollback_body(deploy_sh_text)
        # Same tmp-then-mv pattern as the row-1355 retention write.
        # A plain truncate-in-place `> rollback_timestamp` would leave
        # a half-written breadcrumb visible to a concurrent reader
        # (row 1357 runbook tailing the file while the flip happens).
        assert re.search(r"rollback_timestamp\.tmp\.\$\$", body), (
            "rollback_timestamp must be written via `.tmp.$$` + mv "
            "(atomic rename, never truncate-in-place)"
        )
        assert re.search(r"mv\s+-f[^\n]*rollback_timestamp", body)


# ---------------------------------------------------------------------------
# (7) Escape hatches — dry-run / force / skip preflight
# ---------------------------------------------------------------------------


class TestEscapeHatches:
    def test_dry_run_honored(self, deploy_sh_text: str) -> None:
        body = _rollback_body(deploy_sh_text)
        assert "OMNISIGHT_BLUEGREEN_DRY_RUN" in body, (
            "rollback must honour OMNISIGHT_BLUEGREEN_DRY_RUN=1 so "
            "contract tests and operator sanity checks can plan-print "
            "without mutating the symlink"
        )

    def test_dry_run_exits_before_symlink_flip(self, deploy_sh_text: str) -> None:
        body = _rollback_body(deploy_sh_text)
        dry_run_check = body.find('"${OMNISIGHT_BLUEGREEN_DRY_RUN:-0}" == "1"')
        rollback_invocation = body.find('"$BLUEGREEN_SWITCH" rollback')
        assert dry_run_check >= 0 and rollback_invocation >= 0
        assert dry_run_check < rollback_invocation, (
            "dry-run exit MUST precede the actual `bluegreen_switch.sh "
            "rollback` invocation — otherwise dry-run has already "
            "flipped the symlink"
        )

    def test_retention_force_bypass_exists(self, deploy_sh_text: str) -> None:
        body = _rollback_body(deploy_sh_text)
        assert "OMNISIGHT_ROLLBACK_FORCE" in body, (
            "must honour OMNISIGHT_ROLLBACK_FORCE=1 so operators can "
            "bypass the retention gate after manually recreating "
            "the old color"
        )
        assert "DANGEROUS" in body, (
            "retention bypass must print a DANGEROUS-level warning"
        )

    def test_skip_preflight_bypass_exists(self, deploy_sh_text: str) -> None:
        body = _rollback_body(deploy_sh_text)
        assert "OMNISIGHT_ROLLBACK_SKIP_PREFLIGHT" in body, (
            "must honour OMNISIGHT_ROLLBACK_SKIP_PREFLIGHT=1 for the "
            "rare case where operator is about to `docker compose up` "
            "the previous color right after the symlink flip"
        )


# ---------------------------------------------------------------------------
# (8) Color → port mapping matches compose topology
# ---------------------------------------------------------------------------


class TestColorMapping:
    def test_blue_maps_to_8000(self, deploy_sh_text: str) -> None:
        body = _rollback_body(deploy_sh_text)
        # Same mapping the row-1355 ceremony uses (G2 compose topology).
        assert re.search(r'blue\)\s*RB_PREV_PORT=8000', body), (
            "blue must map to host port 8000 (G2 compose topology)"
        )

    def test_green_maps_to_8001(self, deploy_sh_text: str) -> None:
        body = _rollback_body(deploy_sh_text)
        assert re.search(r'green\)\s*RB_PREV_PORT=8001', body)


# ---------------------------------------------------------------------------
# (9) Caddy reload hook
# ---------------------------------------------------------------------------


class TestCaddyReloadHook:
    def test_reload_cmd_env_honored(self, deploy_sh_text: str) -> None:
        body = _rollback_body(deploy_sh_text)
        assert "OMNISIGHT_BLUEGREEN_CADDY_RELOAD_CMD" in body, (
            "rollback must honour the same operator-supplied reload "
            "hook as the row-1355 ceremony — topology varies (compose / "
            "host systemd / external LB) so we delegate"
        )


# ---------------------------------------------------------------------------
# (10) Runtime smoke — exit codes are correct against a sandbox state dir
# ---------------------------------------------------------------------------


class TestRuntimeBehaviour:
    def test_missing_state_dir_exits_5(self, tmp_path: Path) -> None:
        result = _run_deploy(
            "--rollback",
            env={"OMNISIGHT_BLUEGREEN_DIR": str(tmp_path / "does-not-exist")},
        )
        assert result.returncode == 5, (
            f"missing state dir must exit 5 (got {result.returncode})\n"
            f"stderr={result.stderr!r}"
        )
        assert "primitive missing" in result.stderr

    def test_no_previous_color_exits_2_runtime(self, tmp_path: Path) -> None:
        sandbox = _sandbox_state(tmp_path, active="blue", previous=None,
                                 retention_offset_seconds=None)
        result = _run_deploy(
            "--rollback",
            env={"OMNISIGHT_BLUEGREEN_DIR": str(sandbox)},
        )
        assert result.returncode == 2, (
            f"no previous_color must exit 2 (got {result.returncode})\n"
            f"stderr={result.stderr!r}"
        )
        assert "previous_color" in result.stderr

    def test_retention_expired_exits_8_runtime(self, tmp_path: Path) -> None:
        sandbox = _sandbox_state(
            tmp_path, active="green", previous="blue",
            retention_offset_seconds=-3600,  # expired 1h ago
        )
        result = _run_deploy(
            "--rollback",
            env={"OMNISIGHT_BLUEGREEN_DIR": str(sandbox)},
        )
        assert result.returncode == 8, (
            f"expired retention must exit 8 (got {result.returncode})\n"
            f"stderr={result.stderr!r}"
        )
        assert "retention window EXPIRED" in result.stderr

    def test_retention_force_bypasses_exit_8(self, tmp_path: Path) -> None:
        # With FORCE=1 + a skip-preflight (no real /readyz at port 8000
        # in CI) + DRY_RUN=1, we should exit 0 — the retention gate is
        # bypassed, dry-run short-circuits before the actual flip.
        sandbox = _sandbox_state(
            tmp_path, active="green", previous="blue",
            retention_offset_seconds=-3600,
        )
        result = _run_deploy(
            "--rollback",
            env={
                "OMNISIGHT_BLUEGREEN_DIR": str(sandbox),
                "OMNISIGHT_ROLLBACK_FORCE": "1",
                "OMNISIGHT_BLUEGREEN_DRY_RUN": "1",
            },
        )
        assert result.returncode == 0, (
            f"force+dry-run must exit 0 (got {result.returncode})\n"
            f"stderr={result.stderr!r}\nstdout={result.stdout!r}"
        )
        assert "retention window expired but" in result.stderr

    def test_dry_run_prints_plan_and_exits_0(self, tmp_path: Path) -> None:
        sandbox = _sandbox_state(tmp_path, active="green", previous="blue")
        result = _run_deploy(
            "--rollback",
            env={
                "OMNISIGHT_BLUEGREEN_DIR": str(sandbox),
                "OMNISIGHT_BLUEGREEN_DRY_RUN": "1",
            },
        )
        assert result.returncode == 0, (
            f"dry-run must exit 0 (got {result.returncode})\n"
            f"stderr={result.stderr!r}"
        )
        assert "plan" in result.stdout.lower(), (
            "dry-run must print the rollback plan"
        )
        # Dry-run must NOT mutate the symlink.
        assert (sandbox / "active_upstream.caddy").resolve().name == \
            "upstream-green.caddy"

    def test_noop_when_active_equals_previous(self, tmp_path: Path) -> None:
        # Guardrail: two rollbacks in quick succession (without an
        # intervening cutover) should not ping-pong — the second one
        # sees active == previous and exits 0 as a no-op.
        sandbox = _sandbox_state(
            tmp_path, active="blue", previous="blue",
            retention_offset_seconds=3600,
        )
        result = _run_deploy(
            "--rollback",
            env={"OMNISIGHT_BLUEGREEN_DIR": str(sandbox)},
        )
        assert result.returncode == 0
        assert "no-op" in result.stdout.lower() or "nothing to roll back" in result.stdout.lower()

    def test_full_flip_via_skip_preflight(self, tmp_path: Path) -> None:
        # The end-to-end shape: gates pass, preflight skipped (no real
        # backend in CI), symlink flipped, rollback_timestamp written.
        sandbox = _sandbox_state(tmp_path, active="green", previous="blue")
        result = _run_deploy(
            "--rollback",
            env={
                "OMNISIGHT_BLUEGREEN_DIR": str(sandbox),
                "OMNISIGHT_ROLLBACK_SKIP_PREFLIGHT": "1",
            },
        )
        assert result.returncode == 0, (
            f"full rollback must exit 0 (got {result.returncode})\n"
            f"stderr={result.stderr!r}\nstdout={result.stdout!r}"
        )
        # Post-state: symlink points at blue, active_color is blue,
        # rollback_timestamp breadcrumb exists and is parseable int.
        assert (sandbox / "active_upstream.caddy").resolve().name == \
            "upstream-blue.caddy", "symlink must flip to upstream-blue.caddy"
        assert (sandbox / "active_color").read_text(encoding="utf-8").strip() == "blue"
        ts_file = sandbox / "rollback_timestamp"
        assert ts_file.exists(), "rollback_timestamp breadcrumb must be written"
        ts_val = int(ts_file.read_text(encoding="utf-8").strip())
        assert ts_val > 0


# ---------------------------------------------------------------------------
# (11) Structural + documentation
# ---------------------------------------------------------------------------


class TestStructuralIntegrity:
    def test_bash_syntax_valid(self) -> None:
        result = subprocess.run(
            ["bash", "-n", str(DEPLOY_SH)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, (
            f"deploy.sh has a shell syntax error:\n{result.stderr}"
        )

    def test_header_documents_row_1356(self, deploy_sh_text: str) -> None:
        # The `row 1356` reference must be findable in the file header
        # so a future reader can grep from TODO.md straight to the
        # rollback block.
        assert "row 1356" in deploy_sh_text.lower() or "1356" in deploy_sh_text, (
            "header comment must reference TODO row 1356 so the "
            "rollback flag is traceable from the TODO to the code"
        )

    def test_header_documents_rollback_exit_codes(self, deploy_sh_text: str) -> None:
        # Header block must enumerate the rollback exit codes (2/3/5/8)
        # so operators can triage from the code without re-reading
        # the whole script.
        header = deploy_sh_text.split("set -euo pipefail")[0]
        for code in ("2", "3", "5", "8"):
            assert re.search(rf"\b{code}\b", header), (
                f"header must document rollback exit code {code}"
            )
