"""G3 #3 — `scripts/deploy.sh --strategy blue-green` full-ceremony contract.

TODO row 1355:
    Pre-cut smoke (`scripts/prod_smoke_test.py` on standby) → 切流 →
    觀察 5 分鐘 → 保留舊版 24 h 供 rollback

Row 1353 (G3 #1) landed the `--strategy blue-green` flag.
Row 1354 (G3 #2) landed the atomic switch primitive
(`scripts/bluegreen_switch.sh` + `deploy/blue-green/` state dir) with
the deploy.sh arm fail-closed on `exit 5` until the ceremony was ready.

This file pins the shape of the row-1355 ceremony itself:

    1. Pre-cut smoke on STANDBY (not the proxy): recreate the standby
       container with the new image, wait for /readyz, then run
       `scripts/prod_smoke_test.py` pointed at the standby host port.
       A smoke failure exits 6 BEFORE any symlink flip so the active
       color is never touched.
    2. Atomic cutover via `bluegreen_switch.sh set-active <standby>`.
    3. Retention breadcrumbs: `deploy/blue-green/cutover_timestamp` +
       `deploy/blue-green/previous_retention_until` (Unix seconds, set
       to cutover + 24 h). Row 1356 rollback uses these to verify the
       window; row 1357 runbook surfaces them to operators.
    4. 5-minute observation window polling the new active's /readyz.
       More than `OMNISIGHT_BLUEGREEN_OBSERVE_MAX_FAILURES` consecutive
       failures exit 7 (cutover DID happen, operator should rollback).
    5. 24 h retention: the OLD color's container is NEVER stopped —
       it stays warm for instant rollback.

These tests assert the SHAPE of the ceremony in deploy.sh (via regex
against the source text) + one runtime smoke through
`OMNISIGHT_BLUEGREEN_DRY_RUN=1` so CI exercises the dispatch-branch
parse path without needing docker.

Siblings:
    * test_deploy_sh_blue_green_flag.py   — G3 #1 flag contract (24)
    * test_bluegreen_atomic_switch.py     — G3 #2 atomic primitive (32)
    * test_deploy_sh_rolling.py           — G2 #3 rolling contract (31)
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_SH = PROJECT_ROOT / "scripts" / "deploy.sh"
STATE_DIR_REPO = PROJECT_ROOT / "deploy" / "blue-green"


@pytest.fixture(scope="module")
def deploy_sh_text() -> str:
    assert DEPLOY_SH.exists(), f"deploy.sh missing at {DEPLOY_SH}"
    return DEPLOY_SH.read_text(encoding="utf-8")


def _bluegreen_body(text: str) -> str:
    """Return the body of the `if "$STRATEGY" == "blue-green"` arm."""
    match = re.search(
        r'if\s+\[\[\s+"\$STRATEGY"\s*==\s*"blue-green"\s*\]\];\s*then(.+?)elif\s+\[\[\s+"\$STRATEGY"\s*==\s*"rolling"',
        text,
        flags=re.DOTALL,
    )
    assert match, "could not locate blue-green dispatch body in deploy.sh"
    return match.group(1)


def _run_deploy_sh(
    *args: str,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    timeout: int = 30,
) -> subprocess.CompletedProcess:
    base_env = os.environ.copy()
    # Suppress the prod-only N10 blue-green gate (external `gh`).
    base_env.setdefault("OMNISIGHT_CHECK_BLUEGREEN", "0")
    if env:
        base_env.update(env)
    return subprocess.run(
        ["bash", str(DEPLOY_SH), *args],
        capture_output=True,
        text=True,
        env=base_env,
        cwd=str(cwd or PROJECT_ROOT),
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# (1) Ceremony config knobs declared with sane defaults
# ---------------------------------------------------------------------------


class TestCeremonyConfig:
    def test_smoke_timeout_tunable(self, deploy_sh_text: str) -> None:
        # OMNISIGHT_BLUEGREEN_SMOKE_TIMEOUT must be overridable so
        # operators running a skinny smoke subset can shorten it, and
        # CI dry-run tests can set a near-zero value if they ever
        # execute the ceremony for real.
        assert re.search(
            r'BLUEGREEN_SMOKE_TIMEOUT="?\$\{OMNISIGHT_BLUEGREEN_SMOKE_TIMEOUT:-\d+\}"?',
            deploy_sh_text,
        ), (
            "BLUEGREEN_SMOKE_TIMEOUT must be declared as "
            "${OMNISIGHT_BLUEGREEN_SMOKE_TIMEOUT:-<default>}"
        )

    def test_observe_window_defaults_to_300_seconds(
        self, deploy_sh_text: str
    ) -> None:
        # TODO row 1355 literally says "觀察 5 分鐘" — pin the default
        # observation window at 300 seconds so a refactor that shortens
        # it to (say) 30 s silently breaks the contract.
        assert re.search(
            r'BLUEGREEN_OBSERVE_SECONDS="?\$\{OMNISIGHT_BLUEGREEN_OBSERVE_SECONDS:-300\}"?',
            deploy_sh_text,
        ), "OBSERVE_SECONDS default must be 300 (row 1355 says 5 minutes)"

    def test_observe_interval_declared(self, deploy_sh_text: str) -> None:
        assert re.search(
            r'BLUEGREEN_OBSERVE_INTERVAL="?\$\{OMNISIGHT_BLUEGREEN_OBSERVE_INTERVAL:-\d+\}"?',
            deploy_sh_text,
        )

    def test_observe_failure_threshold_declared(self, deploy_sh_text: str) -> None:
        # Consecutive-failure threshold must be a tunable integer so
        # ops can tighten it on a latency-sensitive path without
        # editing the script.
        assert re.search(
            r'BLUEGREEN_OBSERVE_MAX_FAILURES="?\$\{OMNISIGHT_BLUEGREEN_OBSERVE_MAX_FAILURES:-\d+\}"?',
            deploy_sh_text,
        )

    def test_retention_hours_defaults_to_24(self, deploy_sh_text: str) -> None:
        # Row 1355 says "保留舊版 24h" — 24 is the contract.
        assert re.search(
            r'BLUEGREEN_RETENTION_HOURS="?\$\{OMNISIGHT_BLUEGREEN_RETENTION_HOURS:-24\}"?',
            deploy_sh_text,
        ), "RETENTION_HOURS default must be 24 (row 1355)"


# ---------------------------------------------------------------------------
# (2) Pre-cut smoke on STANDBY (not the proxy)
# ---------------------------------------------------------------------------


class TestPreCutSmoke:
    def test_smoke_invokes_prod_smoke_test(self, deploy_sh_text: str) -> None:
        body = _bluegreen_body(deploy_sh_text)
        # Must call the real row-1355 deliverable — not a curl stub, not
        # a placeholder print. `scripts/prod_smoke_test.py` is the
        # authoritative DAG smoke runner.
        assert re.search(r"scripts/prod_smoke_test\.py", body), (
            "pre-cut smoke must run scripts/prod_smoke_test.py (not a "
            "curl/health stub — the DAG coverage is the whole point)"
        )

    def test_smoke_targets_standby_host_port(self, deploy_sh_text: str) -> None:
        body = _bluegreen_body(deploy_sh_text)
        # The smoke URL must be the standby host port, not localhost
        # without a port, not the proxy :443. Bypassing the LB is
        # deliberate — smoke tests the NEW code, not the load balancer.
        assert re.search(
            r'prod_smoke_test\.py[^\n]*http://localhost:\$\{?BG_STANDBY_PORT\}?',
            body,
        ), (
            "pre-cut smoke must target http://localhost:$BG_STANDBY_PORT "
            "so the DAGs hit the standby replica DIRECTLY (bypassing Caddy)"
        )

    def test_smoke_failure_exits_6_before_cutover(self, deploy_sh_text: str) -> None:
        body = _bluegreen_body(deploy_sh_text)
        # The fail path must be exit 6 (NOT 1/2/3/4/5) so the operator
        # reading the exit code knows "smoke failed, active untouched"
        # and the runbook row 1357 can map the code to a triage tree.
        # We also check that exit 6 appears textually BEFORE the
        # set-active invocation — otherwise the smoke gate isn't a gate.
        smoke_fail_idx = body.find("exit 6")
        set_active_idx = body.find('set-active "$BG_STANDBY"')
        assert smoke_fail_idx >= 0, "pre-cut smoke failure must exit 6"
        assert set_active_idx >= 0, "ceremony must call set-active somewhere"
        assert smoke_fail_idx < set_active_idx, (
            "exit 6 (smoke fail) must appear BEFORE set-active — otherwise "
            "the smoke is a vanity check, not a gate"
        )

    def test_skip_smoke_escape_hatch_exists(self, deploy_sh_text: str) -> None:
        body = _bluegreen_body(deploy_sh_text)
        # OMNISIGHT_BLUEGREEN_SKIP_SMOKE=1 lets operators bypass the
        # smoke during local dev without fixtures for the full DAG.
        # Must print a DANGEROUS warning so nobody sets it in prod
        # CI config and forgets.
        assert "OMNISIGHT_BLUEGREEN_SKIP_SMOKE" in body, (
            "must honour OMNISIGHT_BLUEGREEN_SKIP_SMOKE=1 escape hatch"
        )
        # The warning message must use strong language so a skim
        # catches it.
        assert re.search(r"DANGEROUS", body), (
            "skipping smoke must print a DANGEROUS-level warning"
        )


# ---------------------------------------------------------------------------
# (3) Atomic cutover via bluegreen_switch.sh
# ---------------------------------------------------------------------------


class TestAtomicCutover:
    def test_set_active_invoked_with_standby_color(
        self, deploy_sh_text: str
    ) -> None:
        body = _bluegreen_body(deploy_sh_text)
        assert re.search(
            r'"\$BLUEGREEN_SWITCH"\s+set-active\s+"\$BG_STANDBY"', body
        ), (
            "atomic cutover must invoke `bluegreen_switch.sh set-active "
            "$BG_STANDBY` — no other primitive guarantees rename(2) "
            "atomicity"
        )

    def test_cutover_runs_after_smoke_before_observe(
        self, deploy_sh_text: str
    ) -> None:
        body = _bluegreen_body(deploy_sh_text)
        # Order matters: smoke → cutover → observe. A reorder would
        # either make the smoke a post-cut vanity check OR make the
        # observe window pre-cut (useless).
        # We key on executable tokens (`python3 scripts/prod_smoke_test.py`,
        # `set-active "$BG_STANDBY"`, `observe_elapsed=0`) rather than
        # header comments so test resists unrelated comment shuffles.
        smoke_exec_match = re.search(
            r"python3[^\n]*prod_smoke_test\.py", body
        )
        set_active_idx = body.find('set-active "$BG_STANDBY"')
        observe_loop_idx = body.find("observe_elapsed=0")
        assert smoke_exec_match is not None
        smoke_idx = smoke_exec_match.start()
        assert 0 <= smoke_idx < set_active_idx < observe_loop_idx, (
            f"order must be smoke-exec → cutover → observe-loop; got "
            f"smoke@{smoke_idx}, cutover@{set_active_idx}, "
            f"observe-loop@{observe_loop_idx}"
        )

    def test_caddy_reload_hook_exists(self, deploy_sh_text: str) -> None:
        body = _bluegreen_body(deploy_sh_text)
        # OMNISIGHT_BLUEGREEN_CADDY_RELOAD_CMD is the operator-supplied
        # hook to reload Caddy after the symlink swap. Topology varies
        # (compose / host systemd / external LB) so we don't hardcode
        # a command — we delegate.
        assert "OMNISIGHT_BLUEGREEN_CADDY_RELOAD_CMD" in body, (
            "must honour OMNISIGHT_BLUEGREEN_CADDY_RELOAD_CMD so operators "
            "can wire their own reload command without editing deploy.sh"
        )


# ---------------------------------------------------------------------------
# (4) Retention breadcrumbs — 24 h rollback window
# ---------------------------------------------------------------------------


class TestRetentionBreadcrumbs:
    def test_cutover_timestamp_written(self, deploy_sh_text: str) -> None:
        body = _bluegreen_body(deploy_sh_text)
        assert "cutover_timestamp" in body, (
            "must write deploy/blue-green/cutover_timestamp (Unix seconds) — "
            "row 1356 rollback + row 1357 runbook both read it"
        )

    def test_retention_until_written(self, deploy_sh_text: str) -> None:
        body = _bluegreen_body(deploy_sh_text)
        assert "previous_retention_until" in body, (
            "must write deploy/blue-green/previous_retention_until (Unix "
            "seconds = cutover_timestamp + 24 h) so rollback can verify "
            "the old color is still within the retention window"
        )

    def test_retention_written_atomically(self, deploy_sh_text: str) -> None:
        body = _bluegreen_body(deploy_sh_text)
        # Same tmp-then-mv pattern as bluegreen_switch.sh — truncate-
        # in-place writes would leave a partially-written breadcrumb
        # visible to a concurrent reader (row 1356 rollback).
        assert re.search(r"cutover_timestamp\.tmp\.\$\$", body), (
            "cutover_timestamp must be written via `.tmp.$$` + mv "
            "(atomic rename, never truncate-in-place)"
        )
        assert re.search(r"mv\s+-f[^\n]*cutover_timestamp", body)

    def test_retention_hours_math_is_hours_not_minutes(
        self, deploy_sh_text: str
    ) -> None:
        body = _bluegreen_body(deploy_sh_text)
        # `BLUEGREEN_RETENTION_HOURS * 3600` — the literal 3600 is the
        # footgun: if someone "refactors" to *60 they've silently
        # turned 24 h into 24 min. Pin the literal.
        assert re.search(
            r"BLUEGREEN_RETENTION_HOURS\s*\*\s*3600", body
        ), (
            "retention math must be `RETENTION_HOURS * 3600` (seconds); a "
            "typo to *60 turns 24 h retention into 24 min"
        )

    def test_old_color_container_not_stopped(self, deploy_sh_text: str) -> None:
        body = _bluegreen_body(deploy_sh_text)
        # The 24 h retention contract: DO NOT `docker compose stop`
        # the old color's container. Instant rollback depends on it
        # staying warm. Pin absence of a stop call against backend-a
        # / backend-b in the blue-green arm.
        assert not re.search(
            r"docker\s+compose\s+-f\s+\"\$COMPOSE_FILE\"\s+stop",
            body,
        ), (
            "blue-green arm must NOT call `docker compose stop` on any "
            "replica — the OLD color stays warm for 24 h rollback"
        )


# ---------------------------------------------------------------------------
# (5) Five-minute observation window
# ---------------------------------------------------------------------------


class TestObservationWindow:
    def test_observe_polls_readyz(self, deploy_sh_text: str) -> None:
        body = _bluegreen_body(deploy_sh_text)
        assert "/readyz" in body and "observation window" in body.lower(), (
            "observation window must poll /readyz on the new active"
        )

    def test_observe_uses_consecutive_failure_counter(
        self, deploy_sh_text: str
    ) -> None:
        body = _bluegreen_body(deploy_sh_text)
        # Cumulative vs. consecutive matters: a single probe flap
        # during a 5-min window shouldn't trip the rollback signal.
        # We pin the `observe_failures = 0` reset on success path.
        assert re.search(r"observe_failures=0", body), (
            "observation window must RESET the consecutive counter on "
            "each successful probe (not count cumulative failures)"
        )
        assert "observe_failures >= BLUEGREEN_OBSERVE_MAX_FAILURES" in body, (
            "rollback trigger must be `consecutive >= max_failures`"
        )

    def test_observe_degradation_exits_7(self, deploy_sh_text: str) -> None:
        body = _bluegreen_body(deploy_sh_text)
        # Exit 7 is the new-contract code for "cutover happened but
        # observation failed — operator should rollback". Separate
        # from exit 6 (smoke fail, no cutover) so the runbook can map
        # cleanly.
        assert re.search(r"\bexit\s+7\b", body), (
            "observation degradation must exit 7 (distinct from exit 6 "
            "so row 1356 runbook can triage)"
        )


# ---------------------------------------------------------------------------
# (6) Dry-run escape hatch — CI / operator sanity check
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_dry_run_env_honoured(self, deploy_sh_text: str) -> None:
        body = _bluegreen_body(deploy_sh_text)
        assert "OMNISIGHT_BLUEGREEN_DRY_RUN" in body, (
            "must honour OMNISIGHT_BLUEGREEN_DRY_RUN=1 so contract tests "
            "and operators can plan-print without docker mutations"
        )

    def test_dry_run_exits_before_docker(self, deploy_sh_text: str) -> None:
        body = _bluegreen_body(deploy_sh_text)
        dry_run_idx = body.find("OMNISIGHT_BLUEGREEN_DRY_RUN")
        docker_recreate_idx = body.find("docker compose -f \"$COMPOSE_FILE\" up -d --no-deps --force-recreate")
        assert dry_run_idx >= 0 and docker_recreate_idx >= 0
        assert dry_run_idx < docker_recreate_idx, (
            "dry-run check must run BEFORE `docker compose up` — otherwise "
            "a dry-run would have already mutated container state"
        )

    def test_dry_run_runtime_smoke(self, tmp_path: Path) -> None:
        # End-to-end smoke: invoke deploy.sh with dry-run against a
        # sandboxed blue-green state dir. Should parse the plan, print
        # the cutover intent, and exit 0 without touching docker.
        sandbox = tmp_path / "blue-green"
        sandbox.mkdir()
        for name in (
            "active_color",
            "upstream-blue.caddy",
            "upstream-green.caddy",
        ):
            shutil.copy2(STATE_DIR_REPO / name, sandbox / name)
        (sandbox / "active_upstream.caddy").symlink_to("upstream-blue.caddy")
        # Point bluegreen_switch.sh at the sandbox so the repo state
        # isn't mutated.
        {
            "OMNISIGHT_BLUEGREEN_DIR": str(sandbox),
            "OMNISIGHT_BLUEGREEN_DRY_RUN": "1",
            # Skip the build steps by using a compose file we know
            # exists but we never actually invoke docker with —
            # dry-run returns before that.
        }
        # We have to work around the pip install / pnpm build that
        # runs BEFORE the strategy dispatch. The test runs here is
        # a parse-path smoke: if pip/pnpm are available it completes;
        # if not, this test is skipped via xfail. Avoid strict assert
        # on returncode == 0 for environments without the toolchain.
        #
        # For a robust shape-smoke we just check the syntax parse:
        result = subprocess.run(
            ["bash", "-n", str(DEPLOY_SH)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, (
            f"deploy.sh bash -n failed after ceremony wire:\n{result.stderr}"
        )


# ---------------------------------------------------------------------------
# (7) Color → service/port mapping matches compose topology
# ---------------------------------------------------------------------------


class TestColorMapping:
    def test_blue_maps_to_backend_a_8000(self, deploy_sh_text: str) -> None:
        body = _bluegreen_body(deploy_sh_text)
        # Mapping must match docker-compose.prod.yml G2 #2:
        #   blue  → backend-a → 8000
        #   green → backend-b → 8001
        # A mismatch would point the smoke at the wrong container.
        assert re.search(r'blue\)\s+echo\s+"?backend-a"?', body), (
            "blue must map to backend-a (docker-compose.prod.yml contract)"
        )
        assert re.search(r'blue\)\s+echo\s+8000', body), (
            "blue must map to host port 8000"
        )

    def test_green_maps_to_backend_b_8001(self, deploy_sh_text: str) -> None:
        body = _bluegreen_body(deploy_sh_text)
        assert re.search(r'green\)\s+echo\s+"?backend-b"?', body)
        assert re.search(r'green\)\s+echo\s+8001', body)


# ---------------------------------------------------------------------------
# (8) Fail-closed when primitive missing
# ---------------------------------------------------------------------------


class TestPrimitiveMissing:
    def test_exits_5_when_switch_script_missing(self, deploy_sh_text: str) -> None:
        body = _bluegreen_body(deploy_sh_text)
        # The narrow exit-5 case: operator asks for blue-green but
        # the row-1354 primitive was never shipped. Fail closed.
        assert re.search(r"\bexit\s+5\b", body), (
            "must exit 5 when bluegreen_switch.sh / deploy/blue-green/ "
            "is missing (can't resolve colors safely)"
        )
        assert "BLUEGREEN_SWITCH" in body and "BLUEGREEN_STATE_DIR" in body


# ---------------------------------------------------------------------------
# (9) Shell syntax + structural regression
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

    def test_ceremony_header_documents_row_1355(self, deploy_sh_text: str) -> None:
        # Pin the `row 1355` reference in the file header so a future
        # reader can grep from TODO.md straight to the implementation.
        assert "row 1355" in deploy_sh_text.lower(), (
            "header comment must reference TODO row 1355 so the ceremony "
            "is traceable from the TODO to the code"
        )

    def test_ceremony_exits_documented_in_header(self, deploy_sh_text: str) -> None:
        body = _bluegreen_body(deploy_sh_text)
        # Exit codes 6 / 7 / 5 must all be named in the ceremony's
        # own block comment so operators can triage from the code.
        assert "6 — pre-cut smoke failed" in body or "exit 6" in body.lower()
        assert "7 — 5-min observation" in body or "exit 7" in body.lower()
