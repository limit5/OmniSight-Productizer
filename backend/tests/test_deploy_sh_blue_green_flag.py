"""G3 #1 — `scripts/deploy.sh --strategy blue-green` flag contract tests.

TODO row 1353:
    `scripts/deploy.sh` 新增 `--strategy blue-green` 旗標

The blue-green ceremony itself (atomic active/standby upstream switch,
pre-cut smoke on standby, 5-min observation window, 24 h rollback
retention, `deploy.sh --rollback`, runbook) is the remainder of the G3
deliverable (TODO rows 1354-1357). This file pins just the *flag*:

    * GNU-style `--strategy <value>` + `--strategy=<value>` are parsed
      BEFORE the positional args so the existing
      `[env, git-ref, strategy]` positional contract that the G2
      rolling tests rely on stays intact.
    * `blue-green` joins `rolling` / `systemd` as an accepted strategy.
    * When both flag and positional are supplied, the flag wins
      (GNU-coreutils convention; matches `--verbose`-style overrides).
    * Unknown flags are rejected (not silently swallowed into
      positionals where they would masquerade as an env name).
    * `--strategy` with no argument fails — otherwise a trailing
      newline would silently pick `systemd` from the default resolution
      chain and mask a typo.
    * The blue-green dispatch branch exists and **fails closed** (exit
      5) until the remaining G3 deliverables land — so an operator who
      runs the flag today against prod does NOT accidentally trigger a
      rolling restart dressed up as blue-green.

These tests assert the *shape* of the script + one runtime smoke per
parse path. No Docker / systemd is touched; the blue-green branch is
reached only up to the pre-cutover abort so we never execute pip /
pnpm / git checkout in CI.

Siblings:
    * test_deploy_sh_rolling.py          — G2 #3 rolling contract (31 tests)
    * test_g2_delivery_bundle.py         — G2 #6 cross-file bundle (28 tests)
    * test_dependency_governance.py      — N10 blue-green gate (prod-only)
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_SH = PROJECT_ROOT / "scripts" / "deploy.sh"


@pytest.fixture(scope="module")
def deploy_sh_text() -> str:
    assert DEPLOY_SH.exists(), f"deploy.sh missing at {DEPLOY_SH}"
    return DEPLOY_SH.read_text(encoding="utf-8")


def _run(*args: str, cwd: Path = PROJECT_ROOT) -> subprocess.CompletedProcess:
    """Invoke deploy.sh with args and return stdout/stderr/exitcode.

    We call through `bash` explicitly (instead of relying on the +x
    shebang) so the test passes even on filesystems where the execute
    bit was dropped by e.g. a `git archive` tarball unpack.
    """
    env = os.environ.copy()
    # Suppress the N10 blue-green gate (external `gh` + GitHub API) so
    # parsing tests don't accidentally depend on network / auth state.
    env.setdefault("OMNISIGHT_CHECK_BLUEGREEN", "0")
    return subprocess.run(
        ["bash", str(DEPLOY_SH), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


# ---------------------------------------------------------------------------
# (1) Flag parsing — --strategy is recognized before positionals
# ---------------------------------------------------------------------------


class TestFlagRecognition:
    def test_strategy_flag_parser_exists(self, deploy_sh_text: str) -> None:
        # A dedicated flag loop must exist — otherwise the script would
        # silently treat `--strategy` as $1 (ENV) and fail with the
        # env validator, which is a confusing error path.
        assert re.search(r"--strategy\)", deploy_sh_text), (
            "deploy.sh must case-match `--strategy` (space-separated form)"
        )
        assert re.search(r"--strategy=\*\)", deploy_sh_text), (
            "deploy.sh must case-match `--strategy=*` (equals-separated form, "
            "GNU-coreutils convention)"
        )

    def test_strategy_flag_variable_declared(self, deploy_sh_text: str) -> None:
        # STRATEGY_FLAG is what the dispatcher reads after the parse
        # loop; it must be declared & defaulted so `set -u` doesn't
        # detonate when the flag is absent.
        assert re.search(r'STRATEGY_FLAG="?"?', deploy_sh_text), (
            "STRATEGY_FLAG must be initialized (empty) before the parse loop"
        )

    def test_positional_array_repopulates_dollar_star(
        self, deploy_sh_text: str
    ) -> None:
        # The parse loop collects non-flag args into _positional and
        # then re-does `set -- …` so $1 / $2 / $3 continue to mean
        # ENV / GIT_REF / STRATEGY_ARG downstream. Without this, the
        # entire G2 rolling positional contract would break.
        assert re.search(
            r"_positional\+=\(\s*\"\$1\"\s*\)", deploy_sh_text
        ), "parse loop must accumulate positionals into _positional[]"
        assert re.search(
            r'set\s+--\s+"\$\{_positional\[@\]\+"\$\{_positional\[@\]\}"\}"',
            deploy_sh_text,
        ), (
            "parse loop must repopulate $@ via `set --` using the "
            "set-u-safe `${arr[@]+...}` idiom"
        )


# ---------------------------------------------------------------------------
# (2) Strategy resolution — flag overrides positional & env
# ---------------------------------------------------------------------------


class TestStrategyResolution:
    def test_positional_resolution_chain_preserved(self, deploy_sh_text: str) -> None:
        # The G2 rolling suite asserts this exact pattern. If the flag
        # addition broke it, the rolling tests would fail first — pin
        # it here too so a future refactor sees a clear, blame-able
        # test naming the G3 deliverable.
        assert re.search(
            r'STRATEGY="?\$\{STRATEGY_ARG:-\$\{OMNISIGHT_DEPLOY_STRATEGY:-systemd\}\}"?',
            deploy_sh_text,
        ), (
            "the positional / env resolution chain (STRATEGY_ARG > env > "
            "systemd) must survive the --strategy flag addition"
        )

    def test_flag_overrides_positional_and_env(self, deploy_sh_text: str) -> None:
        # After the positional/env resolution, the flag (if supplied)
        # overrides. This is GNU-coreutils convention: named flags beat
        # positional defaults.
        assert re.search(
            r'if\s+\[\[\s+-n\s+"?\$STRATEGY_FLAG"?\s+\]\]\s*;\s*then\s*\n\s*STRATEGY="\$STRATEGY_FLAG"',
            deploy_sh_text,
        ), (
            "--strategy flag must override the positional / env resolution "
            "(flag wins — `scripts/deploy.sh --strategy blue-green prod v1 rolling` "
            "selects blue-green, not rolling)"
        )

    def test_blue_green_is_accepted_strategy(self, deploy_sh_text: str) -> None:
        # The validator must whitelist blue-green; otherwise the flag
        # parser accepts it but then the validator rejects it, which
        # is a confusing dead-end UX.
        assert re.search(
            r'"\$STRATEGY"\s*!=\s*"rolling"\s*&&\s*"\$STRATEGY"\s*!=\s*"systemd"\s*&&\s*"\$STRATEGY"\s*!=\s*"blue-green"',
            deploy_sh_text,
        ), (
            "strategy validator must accept 'rolling', 'systemd', AND 'blue-green' "
            "— `blue-green` is the G3 HA-03 strategy name (TODO row 1353)"
        )

    def test_validator_error_message_lists_all_three(
        self, deploy_sh_text: str
    ) -> None:
        # Operators grep error messages; ensure the rejection text
        # surfaces all three choices.
        bad_strategy_error = next(
            (
                ln
                for ln in deploy_sh_text.splitlines()
                if "echo" in ln and "strategy must be" in ln.lower()
            ),
            "",
        )
        assert bad_strategy_error, "validator must echo an error on bad strategy"
        lowered = bad_strategy_error.lower()
        for choice in ("rolling", "systemd", "blue-green"):
            assert choice in lowered, (
                f"validator error message must list '{choice}' as a valid option"
            )


# ---------------------------------------------------------------------------
# (3) Dispatch branch — blue-green has a landing pad
# ---------------------------------------------------------------------------


class TestBlueGreenDispatchBranch:
    def test_blue_green_branch_exists(self, deploy_sh_text: str) -> None:
        # There must be an explicit `"$STRATEGY" == "blue-green"` arm.
        # Without it, the validator accepts the flag but the restart
        # block silently falls through to systemd — the exact bug the
        # fail-closed stub is designed to prevent.
        assert re.search(
            r'if\s+\[\[\s+"\$STRATEGY"\s*==\s*"blue-green"\s*\]\]', deploy_sh_text
        ), "deploy.sh must have a dedicated `blue-green` dispatch arm"

    def test_blue_green_branch_runs_before_rolling(self, deploy_sh_text: str) -> None:
        # Order matters: the blue-green guard (exit 5) must execute
        # BEFORE the rolling branch. If rolling ran first the script
        # would start tearing down backend-a for a rolling restart
        # even when the operator asked for blue-green.
        bg_idx = deploy_sh_text.find('"$STRATEGY" == "blue-green"')
        rolling_idx = deploy_sh_text.find('"$STRATEGY" == "rolling"')
        assert bg_idx >= 0 and rolling_idx >= 0
        assert bg_idx < rolling_idx, (
            "blue-green dispatch must be checked before rolling (if-elif "
            "order) so the fail-closed stub intercepts before rolling's "
            "`docker compose stop backend-a` runs"
        )

    def test_blue_green_fails_closed_until_ceremony_wired(
        self, deploy_sh_text: str
    ) -> None:
        # Pull the blue-green branch body and assert the deliberate
        # `exit 5`. The remainder of G3 (rows 1354-1357) will replace
        # this fail-closed stub with the real ceremony; until then, the
        # contract is: flag accepted, but NO upstream touch.
        match = re.search(
            r'if\s+\[\[\s+"\$STRATEGY"\s*==\s*"blue-green"\s*\]\];\s*then(.+?)elif\s+\[\[\s+"\$STRATEGY"\s*==\s*"rolling"',
            deploy_sh_text,
            flags=re.DOTALL,
        )
        assert match, "could not locate blue-green dispatch body"
        body = match.group(1)
        assert re.search(r"\bexit\s+5\b", body), (
            "blue-green branch must `exit 5` (ENOSYS-like) until the "
            "remaining G3 ceremony deliverables land — never silently "
            "fall through to rolling/systemd"
        )

    def test_blue_green_checks_compose_file_exists(
        self, deploy_sh_text: str
    ) -> None:
        # Same pre-flight the rolling branch does (exit 4). Catches a
        # missing docker-compose.prod.yml before any other work.
        match = re.search(
            r'if\s+\[\[\s+"\$STRATEGY"\s*==\s*"blue-green"\s*\]\];\s*then(.+?)elif\s+\[\[',
            deploy_sh_text,
            flags=re.DOTALL,
        )
        assert match
        body = match.group(1)
        assert re.search(
            r'if\s+\[\[\s+!\s+-f\s+"?\$ROOT/\$COMPOSE_FILE"?\s+\]\]', body
        ), "blue-green branch must pre-flight the compose file like rolling"
        assert re.search(r"exit\s+4", body), (
            "missing compose file in blue-green mode must use the same "
            "exit 4 as rolling so CI / simulate.sh match on one code"
        )

    def test_blue_green_branch_references_followup_rows(
        self, deploy_sh_text: str
    ) -> None:
        # An operator reading the fail-closed stderr output should
        # know *why* it failed and *where* the remaining wiring is.
        # Pin the references so the stub is self-documenting and
        # future-me can't accidentally strip the breadcrumbs.
        match = re.search(
            r'if\s+\[\[\s+"\$STRATEGY"\s*==\s*"blue-green"\s*\]\];\s*then(.+?)elif\s+\[\[',
            deploy_sh_text,
            flags=re.DOTALL,
        )
        assert match
        body = match.group(1)
        # Reference to remaining TODO rows (1354-1357) — format tolerant.
        assert re.search(r"135[4-7]", body), (
            "blue-green fail-closed output must reference the remaining "
            "G3 TODO rows (1354-1357) so operators can trace next steps"
        )


# ---------------------------------------------------------------------------
# (4) Usage string — blue-green must be discoverable
# ---------------------------------------------------------------------------


class TestUsageDocumentation:
    def test_header_comment_documents_blue_green(self, deploy_sh_text: str) -> None:
        # The `# Usage:` comment at the top of the script is what
        # operators scan when `-h` isn't implemented yet. blue-green
        # must show up there.
        first_200 = "\n".join(deploy_sh_text.splitlines()[:80])
        assert "blue-green" in first_200.lower(), (
            "the Usage: comment block must mention blue-green so operators "
            "discover the flag without grepping the whole file"
        )

    def test_runtime_usage_error_mentions_blue_green(
        self, deploy_sh_text: str
    ) -> None:
        # When run with no args, the stderr usage hint must list
        # blue-green as a strategy — otherwise it's a hidden flag.
        usage_echoes = [
            ln
            for ln in deploy_sh_text.splitlines()
            if "echo" in ln and "usage:" in ln.lower()
        ]
        assert usage_echoes, "deploy.sh must echo a usage string on bad args"
        joined = " ".join(usage_echoes).lower()
        assert "blue-green" in joined, (
            "runtime usage: error must mention blue-green so `scripts/deploy.sh` "
            "with no args surfaces the flag"
        )
        assert "--strategy" in joined, (
            "runtime usage: error must show the --strategy flag form"
        )


# ---------------------------------------------------------------------------
# (5) Runtime smoke — actually invoke bash and check the parse paths
# ---------------------------------------------------------------------------


class TestRuntimeParseBehaviour:
    def test_bash_syntax_valid(self) -> None:
        # A broken syntax (e.g. missing `fi`) would take down every
        # downstream test and the deploy itself. `bash -n` parses
        # without executing — cheap smoke, catches 90% of fat-finger
        # regressions.
        result = subprocess.run(
            ["bash", "-n", str(DEPLOY_SH)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, (
            f"deploy.sh has a shell syntax error:\n{result.stderr}"
        )

    def test_no_args_exits_with_usage(self) -> None:
        result = _run()
        assert result.returncode == 1
        assert "usage:" in result.stderr.lower()
        assert "blue-green" in result.stderr.lower()

    def test_strategy_without_value_fails(self) -> None:
        # `--strategy` at end of args (no value) must fail explicitly,
        # not silently consume whatever came before as its value or
        # fall through to the default `systemd`.
        result = _run("--strategy")
        assert result.returncode == 1
        assert "requires a value" in result.stderr.lower()

    def test_unknown_flag_rejected(self) -> None:
        result = _run("--not-a-flag", "prod")
        assert result.returncode == 1
        assert "unknown flag" in result.stderr.lower()

    def test_unknown_strategy_value_rejected(self) -> None:
        result = _run("--strategy", "bogus", "prod")
        assert result.returncode == 1
        assert "strategy must be" in result.stderr.lower()
        assert "blue-green" in result.stderr.lower()

    def test_blue_green_strategy_accepted_by_validator(self) -> None:
        # We can't let the full deploy execute (it would pip install,
        # pnpm build, etc. in CI), but we can assert the validator
        # doesn't reject blue-green — i.e. the exit code is NOT 1
        # from the strategy rejector, and it either reaches the
        # fail-closed stub (exit 5) or the N10 gate / build / cutover
        # further downstream. An operator-style invocation against
        # staging (which skips N10) should land at exit 5 reliably.
        result = _run("--strategy", "blue-green", "staging")
        # It MUST NOT be exit 1 (the strategy rejector); it should be
        # either 5 (fail-closed cutover stub reached) or a non-1 code
        # from a downstream step (build failure in non-dev envs).
        assert result.returncode != 1 or "strategy must be" not in result.stderr.lower(), (
            f"--strategy blue-green was rejected by the validator:\n"
            f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
        )

    def test_strategy_equals_form_also_accepted(self) -> None:
        # `--strategy=blue-green` (equals form) must behave the same
        # as `--strategy blue-green` (space form). Lots of CI systems
        # pass args glued together; both must work.
        result = _run("--strategy=blue-green", "bogus-env")
        # We deliberately pass a bad env after a valid strategy: if
        # the flag parser consumed `--strategy=blue-green` correctly,
        # the script should fail at the env validator (exit 1, "env
        # must be") — NOT at the strategy validator.
        assert result.returncode == 1
        assert "env must be" in result.stderr.lower(), (
            f"--strategy=blue-green equals-form was not parsed correctly; "
            f"stderr={result.stderr!r}"
        )


# ---------------------------------------------------------------------------
# (6) Legacy regression — G2 rolling contract must still hold
# ---------------------------------------------------------------------------


class TestLegacyContractPreserved:
    def test_rolling_positional_arg_still_works_end_to_end(
        self, deploy_sh_text: str
    ) -> None:
        # If the flag parse loop accidentally consumed positionals
        # the old `scripts/deploy.sh prod v0.2.0 rolling` form would
        # break. The test_deploy_sh_rolling.py suite covers the shape
        # side; this pins the literal positional declaration after
        # the flag loop.
        assert "STRATEGY_ARG=${3:-}" in deploy_sh_text
        assert "ENV=${1:-}" in deploy_sh_text
        assert "GIT_REF=${2:-}" in deploy_sh_text
        # The three positionals must come AFTER the flag parse loop;
        # otherwise the reorder wouldn't take effect.
        parse_loop_idx = deploy_sh_text.find("while [[ $# -gt 0 ]]")
        env_assign_idx = deploy_sh_text.find("ENV=${1:-}")
        assert parse_loop_idx >= 0 and env_assign_idx >= 0
        assert parse_loop_idx < env_assign_idx, (
            "flag parse loop must run BEFORE ENV/GIT_REF/STRATEGY_ARG "
            "are read from $1/$2/$3 — otherwise repopulation is a no-op"
        )

    def test_n10_bluegreen_gate_block_untouched(self, deploy_sh_text: str) -> None:
        # The N10 blue-green gate (scripts/check_bluegreen_gate.py)
        # is orthogonal to this flag — it's the prod-deploy policy
        # guard. Make sure the flag addition didn't delete it.
        assert "scripts/check_bluegreen_gate.py" in deploy_sh_text
        assert 'if [[ "$ENV" == "prod" ]]; then' in deploy_sh_text

    def test_systemd_default_when_no_flag_no_positional(
        self, deploy_sh_text: str
    ) -> None:
        # With no flag and no positional, STRATEGY must still resolve
        # to `systemd` (the legacy single-replica default). A future
        # refactor that defaults to blue-green would silently break
        # every non-compose host.
        assert re.search(
            r'STRATEGY=.*:-systemd\}', deploy_sh_text
        ), "default strategy must remain `systemd`, not `rolling` or `blue-green`"
