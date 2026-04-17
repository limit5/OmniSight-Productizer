"""G2 #3 — `scripts/deploy.sh` rolling-restart contract tests.

TODO row 1347:
    `scripts/deploy.sh` 改為 rolling：取下 A → 重啟 → `/readyz` pass →
    取下 B → 重啟

The legacy single-replica systemd flow must stay intact (operators
without the docker-compose dual-replica topology still rely on it), so
rolling mode is opt-in via either:

    * third positional arg `rolling`
    * env `OMNISIGHT_DEPLOY_STRATEGY=rolling`

These tests assert the *shape* of the script — no Docker / systemd
runtime required in CI. They complement, not replace, the G2 #5 soak
test which puts real traffic through Caddy during a rolling restart.

Siblings:
    * test_compose_dual_backend_replicas.py — G2 #2 compose contract
    * test_reverse_proxy_caddyfile.py      — G2 #1 Caddy contract
    * test_dependency_governance.py::test_n10_deploy_sh_*  — N10 gate
"""

from __future__ import annotations

import re
import stat
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_SH = PROJECT_ROOT / "scripts" / "deploy.sh"
COMPOSE_FILE = PROJECT_ROOT / "docker-compose.prod.yml"
CADDYFILE = PROJECT_ROOT / "deploy" / "reverse-proxy" / "Caddyfile"


@pytest.fixture(scope="module")
def deploy_sh_text() -> str:
    assert DEPLOY_SH.exists(), f"deploy.sh missing at {DEPLOY_SH}"
    return DEPLOY_SH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# (1) File-level hygiene
# ---------------------------------------------------------------------------


class TestFileHygiene:
    def test_deploy_sh_is_executable(self) -> None:
        mode = DEPLOY_SH.stat().st_mode
        assert mode & stat.S_IXUSR, (
            "scripts/deploy.sh must be executable — operators chmod it "
            "once at repo clone and expect it to stay that way"
        )

    def test_deploy_sh_uses_strict_mode(self, deploy_sh_text: str) -> None:
        # `set -euo pipefail` is the entire reason the script is safe to
        # run unattended — a silent bad substitution in rolling mode would
        # otherwise leave one replica down and the other taking 100% of
        # traffic until the next human notices.
        assert "set -euo pipefail" in deploy_sh_text

    def test_deploy_sh_bash_shebang(self, deploy_sh_text: str) -> None:
        first_line = deploy_sh_text.splitlines()[0]
        assert first_line.startswith("#!") and "bash" in first_line, (
            "deploy.sh must declare a bash shebang — POSIX /bin/sh lacks "
            "the `[[` / arrays / `$(seq …)` the rolling flow depends on"
        )


# ---------------------------------------------------------------------------
# (2) Strategy selection — legacy mode stays default
# ---------------------------------------------------------------------------


class TestStrategySelection:
    def test_rolling_strategy_is_opt_in(self, deploy_sh_text: str) -> None:
        # Default must remain systemd so hosts that never adopted the
        # dual-replica compose topology aren't silently broken.
        assert re.search(
            r'STRATEGY="?\$\{STRATEGY_ARG:-\$\{OMNISIGHT_DEPLOY_STRATEGY:-systemd\}\}"?',
            deploy_sh_text,
        ), (
            "rolling mode must be opt-in (default strategy = systemd). "
            "The resolution chain should be: positional arg > env var > systemd"
        )

    def test_third_positional_arg_is_strategy(self, deploy_sh_text: str) -> None:
        assert "STRATEGY_ARG=${3:-}" in deploy_sh_text, (
            "the third positional arg must be `strategy` (rolling|systemd) "
            "so operators can type `scripts/deploy.sh prod v0.2.0 rolling`"
        )

    def test_strategy_validation_rejects_unknown(self, deploy_sh_text: str) -> None:
        # An operator typing `scripts/deploy.sh prod v0.2.0 rollng` must
        # not silently fall through to systemd; strict validation catches
        # the typo before anything touches the cluster.
        assert re.search(
            r'"\$STRATEGY"\s*!=\s*"rolling"\s*&&\s*"\$STRATEGY"\s*!=\s*"systemd"',
            deploy_sh_text,
        ), (
            "deploy.sh must reject any strategy value other than "
            "'rolling' or 'systemd'"
        )

    def test_usage_string_documents_strategy(self, deploy_sh_text: str) -> None:
        # If an operator runs deploy.sh with no args they should see the
        # rolling mode in the usage hint — otherwise it's a hidden feature.
        assert "rolling" in deploy_sh_text.lower()
        # The runtime usage error (echo'd to stderr when args are missing)
        # must mention the strategy positional. Filter to echo lines only
        # so we don't pick up the opening `# Usage:` comment block.
        usage_echo = next(
            (
                ln for ln in deploy_sh_text.splitlines()
                if "usage:" in ln.lower() and "echo" in ln
            ),
            "",
        )
        assert usage_echo, "deploy.sh must echo a usage string on bad args"
        lowered = usage_echo.lower()
        assert "rolling" in lowered or "strategy" in lowered, (
            "usage: error message must mention the rolling/strategy arg"
        )


# ---------------------------------------------------------------------------
# (3) Rolling flow — the canonical A → drain → recreate → B sequence
# ---------------------------------------------------------------------------


class TestRollingFlow:
    def test_rolling_restart_helper_function_exists(self, deploy_sh_text: str) -> None:
        # Single source of truth for "what does it mean to roll a replica".
        # Having a named function (vs inline duplication for A and B) makes
        # it testable and makes the A-then-B invariant obvious.
        assert re.search(
            r"rolling_restart_replica\s*\(\s*\)\s*\{",
            deploy_sh_text,
        ), "deploy.sh must define a `rolling_restart_replica()` helper"

    def test_rolling_touches_both_replicas(self, deploy_sh_text: str) -> None:
        calls = re.findall(
            r"rolling_restart_replica\s+(\"?)backend-([ab])\1\s+(\d+)",
            deploy_sh_text,
        )
        services = [f"backend-{m[1]}" for m in calls]
        ports = [m[2] for m in calls]
        assert "backend-a" in services and "backend-b" in services, (
            "rolling flow must call rolling_restart_replica for both "
            "backend-a and backend-b"
        )
        assert "8000" in ports and "8001" in ports, (
            "rolling flow must address backend-a on 8000 and backend-b on 8001"
        )

    def test_rolling_runs_a_before_b(self, deploy_sh_text: str) -> None:
        # Order matters: A → B guarantees at most one replica is ever in
        # the "draining" state. Flipping the order (B → A) is harmless
        # in isolation but churns through prometheus `backend` DNS alias
        # (which is on A) — keep A first so scrape continuity is stable.
        a_match = re.search(
            r"rolling_restart_replica\s+(\"?)backend-a\1\s+8000", deploy_sh_text
        )
        b_match = re.search(
            r"rolling_restart_replica\s+(\"?)backend-b\1\s+8001", deploy_sh_text
        )
        assert a_match and b_match
        assert a_match.start() < b_match.start(), (
            "rolling flow must touch backend-a BEFORE backend-b (never "
            "both simultaneously, never B first)"
        )

    def test_rolling_never_parallelizes(self, deploy_sh_text: str) -> None:
        # No `&` at end of rolling_restart_replica lines — background-ing
        # the first call would take both replicas down at the same time
        # and turn the deploy into a cold restart, violating the entire
        # rolling invariant.
        for line in deploy_sh_text.splitlines():
            stripped = line.strip()
            if "rolling_restart_replica" in stripped and not stripped.startswith("#"):
                # Skip function-definition line.
                if stripped.startswith("rolling_restart_replica()") or "{" in stripped and "()" in stripped:
                    continue
                assert not stripped.rstrip().endswith("&"), (
                    f"rolling_restart_replica call must not be backgrounded: {line!r}"
                )


# ---------------------------------------------------------------------------
# (4) Inside the helper — drain → recreate → wait /readyz
# ---------------------------------------------------------------------------


class TestRollingReplicaHelper:
    @pytest.fixture(scope="class")
    def helper_body(self, deploy_sh_text: str = None) -> str:
        # Parametrize via pytest dependency injection if needed.
        text = DEPLOY_SH.read_text(encoding="utf-8")
        match = re.search(
            r"rolling_restart_replica\s*\(\s*\)\s*\{(.+?)^\}",
            text,
            flags=re.DOTALL | re.MULTILINE,
        )
        assert match, "could not locate rolling_restart_replica body"
        return match.group(1)

    def test_helper_takes_service_and_port(self, helper_body: str) -> None:
        # Positional args so the caller can pass `backend-a 8000`.
        assert 'local svc="$1"' in helper_body
        assert 'local port="$2"' in helper_body

    def test_helper_drains_via_docker_compose_stop(self, helper_body: str) -> None:
        # `docker compose stop` sends SIGTERM then waits the timeout
        # before SIGKILL — that's what lets backend/lifecycle.py drain.
        # We must NOT use `docker compose down` (removes the volume
        # network), `docker compose kill` (SIGKILL, skips drain), or
        # `docker stop` (operates on container id, brittle across
        # compose project renames).
        assert re.search(r"docker\s+compose\s+-f\s+\"?\$COMPOSE_FILE\"?\s+stop", helper_body), (
            "helper must use `docker compose -f $COMPOSE_FILE stop …` to "
            "trigger SIGTERM drain (not down / kill / raw docker stop)"
        )
        assert "--timeout" in helper_body, (
            "`docker compose stop` without --timeout falls back to the 10s "
            "default, which is SHORTER than lifecycle.py's 30s drain budget"
        )

    def test_helper_confirms_drain_before_recreate(self, helper_body: str) -> None:
        # The confirmation loop is the whole point of "取下 A" — we don't
        # just fire SIGTERM and hope; we poll /readyz until it stops
        # answering 200 so Caddy has definitely ejected the replica.
        assert "/readyz" in helper_body
        # Polling loop against the replica's own host-exposed port.
        assert re.search(
            r'for\s+\w+\s+in\s+\$\(\s*seq\s+1\s+"?\$(ROLL_DRAIN_TIMEOUT|\{ROLL_DRAIN_TIMEOUT\})"?\s*\)',
            helper_body,
        ), "helper must poll for drain confirmation in a bounded loop"

    def test_helper_recreates_with_no_deps_force_recreate(
        self, helper_body: str
    ) -> None:
        # --no-deps keeps the frontend up (depends_on both replicas would
        # otherwise restart the whole app every rolling step).
        # --force-recreate ensures new env-file / image values take effect
        # even when the tag didn't change.
        up_line = next(
            (
                ln for ln in helper_body.splitlines()
                if "docker compose" in ln
                and re.search(r"\bup\b", ln)
                and not ln.lstrip().startswith("#")
            ),
            "",
        )
        assert up_line, "helper must call `docker compose up -d …` on the replica"
        assert "--no-deps" in up_line, (
            "recreate must use --no-deps to avoid rippling into frontend / prometheus"
        )
        assert "--force-recreate" in up_line, (
            "recreate must use --force-recreate so new image/env takes effect"
        )
        assert "-d" in up_line.split(), "recreate must use -d (detached)"

    def test_helper_waits_for_readyz_200(self, helper_body: str) -> None:
        # The `/readyz pass` step in TODO row 1347. We accept only 200
        # responses — curl -sf fails on non-2xx so this is implicit.
        assert re.search(r'curl\s+-[a-z]*f[a-z]*\b.*"?\$ready_url"?', helper_body) \
            or re.search(r'curl\s+-[a-z]*f[a-z]*\b.*/readyz', helper_body), (
            "helper must use `curl -sf` (or -f) so non-2xx /readyz is "
            "treated as failure, not success"
        )
        # Bounded wait — never infinite. Operators must see the abort.
        assert "ROLL_READY_TIMEOUT" in helper_body, (
            "helper must bound the readiness wait with ROLL_READY_TIMEOUT"
        )

    def test_helper_aborts_on_ready_timeout_before_touching_other(
        self, helper_body: str
    ) -> None:
        # This is the load-bearing invariant: if A never comes back, we
        # must exit non-zero BEFORE the caller moves on to B. Otherwise
        # a bad image takes both replicas down in sequence → outage.
        assert re.search(r"exit\s+3", helper_body), (
            "helper must `exit 3` on readiness timeout so the caller "
            "never proceeds to restart the second replica"
        )

    def test_helper_surfaces_triage_hint(self, helper_body: str) -> None:
        # When we abort on readiness timeout the operator will look at
        # stderr first. Print the exact `docker compose logs …` command
        # they should run next — the triage path is self-documenting.
        assert "docker compose" in helper_body
        assert "logs" in helper_body, (
            "abort path should point operators at `docker compose logs` "
            "for the failing replica"
        )


# ---------------------------------------------------------------------------
# (5) Tunables — operator knobs are env-overridable
# ---------------------------------------------------------------------------


class TestRollingTunables:
    @pytest.mark.parametrize(
        "var,default",
        [
            # Drain timeout must match or exceed lifecycle.py's 30s budget.
            ("OMNISIGHT_ROLL_DRAIN_TIMEOUT", "35"),
            # Ready timeout must cover cold-start (deps install + migrations
            # + alembic head check). 120s is the compose healthcheck
            # start_period (20s) × a generous multiplier.
            ("OMNISIGHT_ROLL_READY_TIMEOUT", "120"),
            ("OMNISIGHT_ROLL_POLL_INTERVAL", "2"),
        ],
    )
    def test_rolling_knob_env_overridable(
        self, deploy_sh_text: str, var: str, default: str
    ) -> None:
        # `VAR=${ENV_VAR:-default}` idiom so ops can override on the
        # command line without editing the file (`OMNISIGHT_ROLL_READY_TIMEOUT=240 scripts/deploy.sh prod v0.2.0 rolling`).
        assert re.search(
            rf"\b\w+=\"?\$\{{{var}:-{default}\}}\"?",
            deploy_sh_text,
        ), (
            f"rolling-mode knob {var} must be env-overridable with default {default}"
        )

    def test_compose_file_is_env_overridable(self, deploy_sh_text: str) -> None:
        # Operators on forked topologies (`docker-compose.ha.yml`) need
        # to point the rolling flow at their file without editing deploy.sh.
        assert re.search(
            r'COMPOSE_FILE="?\$\{OMNISIGHT_COMPOSE_FILE:-docker-compose\.prod\.yml\}"?',
            deploy_sh_text,
        )

    def test_drain_timeout_covers_lifecycle_budget(self, deploy_sh_text: str) -> None:
        # backend/lifecycle.py DEFAULT_DRAIN_TIMEOUT_SECONDS = 30.0. If
        # the deploy-side timeout is shorter, we'd SIGKILL mid-drain and
        # in-flight requests would 502. Pin the default to >= 30.
        match = re.search(
            r'OMNISIGHT_ROLL_DRAIN_TIMEOUT:-(\d+)', deploy_sh_text
        )
        assert match, "ROLL_DRAIN_TIMEOUT default missing"
        assert int(match.group(1)) >= 30, (
            "rolling drain timeout default must be >= lifecycle.py's 30s "
            "drain budget, otherwise in-flight requests get SIGKILL'd"
        )


# ---------------------------------------------------------------------------
# (6) Pre-flight — rolling mode needs the compose file
# ---------------------------------------------------------------------------


class TestPreflight:
    def test_missing_compose_file_exits_early(self, deploy_sh_text: str) -> None:
        # A fresh clone without docker-compose.prod.yml would otherwise
        # `docker compose stop` against an empty project and report
        # "no such service". Catch it up front with a specific exit code
        # that `simulate.sh` and CI can match on.
        assert re.search(
            r'if\s+\[\[\s+!\s+-f\s+"?\$ROOT/\$COMPOSE_FILE"?\s+\]\]',
            deploy_sh_text,
        ), "rolling mode must check the compose file exists before proceeding"
        assert re.search(r"exit\s+4", deploy_sh_text), (
            "missing compose file should abort with a distinct exit code (4)"
        )


# ---------------------------------------------------------------------------
# (7) Smoke stage — rolling mode probes BOTH replicas
# ---------------------------------------------------------------------------


class TestRollingSmokeTest:
    def test_rolling_smoke_probes_both_ports(self, deploy_sh_text: str) -> None:
        # In systemd mode we only have one backend to smoke; in rolling
        # mode a silent half-broken pool (A healthy, B 503) is our worst
        # nightmare because round-robin hides it. Probe both ports so
        # CI and operators notice immediately.
        smoke_idx = deploy_sh_text.lower().find("smoke test")
        assert smoke_idx >= 0, "smoke test section must exist"
        smoke_tail = deploy_sh_text[smoke_idx:]
        # Both ports must be referenced in the smoke section of the script.
        # `for port in 8000 8001` is the canonical form in the script.
        assert re.search(
            r"for\s+port\s+in\s+8000\s+8001", smoke_tail
        ), (
            "rolling smoke stage must loop over both replica ports (8000, 8001)"
        )


# ---------------------------------------------------------------------------
# (8) Legacy-mode regression — systemd path must not be broken
# ---------------------------------------------------------------------------


class TestLegacyRegression:
    def test_systemd_branch_still_calls_systemctl(self, deploy_sh_text: str) -> None:
        # Don't let the rolling refactor accidentally delete the
        # single-replica code path — hosts without compose still depend
        # on it for staging deploys.
        assert "systemctl restart" in deploy_sh_text
        assert "BACKEND_UNIT" in deploy_sh_text
        assert "FRONTEND_UNIT" in deploy_sh_text

    def test_systemd_branch_still_uses_api_v1_health(self, deploy_sh_text: str) -> None:
        # Legacy smoke hits /api/v1/health (liveness) — the single-replica
        # systemd flow predates /readyz and we keep it to avoid surprise
        # regressions on existing hosts.
        assert "/api/v1/health" in deploy_sh_text

    def test_n10_bluegreen_gate_preserved(self, deploy_sh_text: str) -> None:
        # The N10 blue-green gate (test_dependency_governance.py) is a
        # prod-only guard that must survive the rolling-mode refactor.
        assert 'if [[ "$ENV" == "prod" ]]; then' in deploy_sh_text
        assert "scripts/check_bluegreen_gate.py" in deploy_sh_text

    def test_db_backup_still_happens(self, deploy_sh_text: str) -> None:
        # WAL-safe online backup must run for both strategies — a rolling
        # restart can still fall back to single-replica if B fails to
        # start, and the backup is what lets an operator rewind.
        assert 'sqlite3 "$DB_PATH" ".backup' in deploy_sh_text


# ---------------------------------------------------------------------------
# (9) Cross-file consistency — rolling mode matches Caddy + compose
# ---------------------------------------------------------------------------


class TestCrossFileConsistency:
    def test_rolling_ports_match_compose_host_mappings(
        self, deploy_sh_text: str
    ) -> None:
        # The rolling flow polls localhost:8000 / localhost:8001 — that
        # only works if compose publishes those host ports. This is the
        # same contract test_compose_dual_backend_replicas.py enforces
        # from the compose side, re-checked here to catch drift if the
        # compose file changes first.
        compose_text = COMPOSE_FILE.read_text(encoding="utf-8")
        assert '"8000:8000"' in compose_text or "8000:8000" in compose_text
        assert '"8001:8001"' in compose_text or "8001:8001" in compose_text
        # deploy.sh probes both ports on localhost in rolling mode.
        assert "localhost:8000" in deploy_sh_text or "localhost:${port}" in deploy_sh_text
        assert "localhost:8001" in deploy_sh_text or "localhost:${port}" in deploy_sh_text

    def test_rolling_relies_on_caddy_readyz_eject(
        self, deploy_sh_text: str
    ) -> None:
        # The deploy script doesn't talk to Caddy directly — it relies on
        # Caddy's active /readyz health probe to eject a draining replica.
        # This test pins the invariant: Caddyfile references /readyz.
        caddyfile = CADDYFILE.read_text(encoding="utf-8")
        assert "health_uri /readyz" in caddyfile, (
            "Caddyfile must keep an active /readyz health probe — the "
            "deploy.sh rolling flow depends on it for automatic eject"
        )
