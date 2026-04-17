"""G3 #2 — `scripts/bluegreen_switch.sh` + `deploy/blue-green/` atomic switch contract.

TODO row 1354:
    維護 active/standby symlink 或 proxy upstream 切換（atomic）

The blue-green strategy flag (TODO row 1353, landed in G3 #1) is the
operator-facing entry point; this row ships the MECHANISM underneath:
an atomic active/standby switch that maintains a symlinked Caddy
upstream snippet + a plain-file source-of-record, so that the running
proxy can be flipped from one color to the other in a single
``rename(2)`` call with no half-written window.

Deliverables pinned by this file:

    * ``deploy/blue-green/`` directory with four artefacts:
        - ``active_color``          plain-file source-of-record (blue|green)
        - ``upstream-blue.caddy``   Caddy snippet → backend-a:8000
        - ``upstream-green.caddy``  Caddy snippet → backend-b:8001
        - ``active_upstream.caddy`` symlink → upstream-<color>.caddy
    * ``scripts/bluegreen_switch.sh`` with four subcommands:
        - ``status``       → print active/standby/symlink/previous
        - ``switch``       → flip active ↔ standby (atomic)
        - ``set-active``   → set active color explicitly (idempotent)
        - ``rollback``     → flip to the recorded previous color
    * Atomicity: every state-altering op must use the write-tmp +
      ``mv -Tf`` / ``mv -f`` dance so ``rename(2)`` is the only
      thing an outside reader ever sees.
    * Caddy snippets use a shared name ``(active_upstream_rp)`` so
      the consumer Caddyfile never needs to know which color is
      live — the symlink abstracts that decision.
    * ``scripts/deploy.sh --strategy blue-green`` shows current
      state (via ``bluegreen_switch.sh status``) but STILL fails
      closed (``exit 5``) because the full ceremony (rows 1355-
      1357) isn't wired.

Siblings:
    * test_deploy_sh_blue_green_flag.py  — G3 #1 flag contract (24)
    * test_deploy_sh_rolling.py          — G2 #3 rolling contract (31)
    * test_reverse_proxy_caddyfile.py    — G2 #1 Caddy contract
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SWITCH_SH = PROJECT_ROOT / "scripts" / "bluegreen_switch.sh"
DEPLOY_SH = PROJECT_ROOT / "scripts" / "deploy.sh"
STATE_DIR = PROJECT_ROOT / "deploy" / "blue-green"
BLUE_SNIPPET = STATE_DIR / "upstream-blue.caddy"
GREEN_SNIPPET = STATE_DIR / "upstream-green.caddy"
ACTIVE_SYMLINK = STATE_DIR / "active_upstream.caddy"
ACTIVE_COLOR_FILE = STATE_DIR / "active_color"


@pytest.fixture(scope="module")
def switch_sh_text() -> str:
    assert SWITCH_SH.exists(), f"bluegreen_switch.sh missing at {SWITCH_SH}"
    return SWITCH_SH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def deploy_sh_text() -> str:
    assert DEPLOY_SH.exists()
    return DEPLOY_SH.read_text(encoding="utf-8")


@pytest.fixture()
def sandbox(tmp_path: Path) -> Path:
    """Fresh copy of deploy/blue-green/ into a tmp dir.

    We never mutate the committed state directly — all mutation tests
    point the script at ``OMNISIGHT_BLUEGREEN_DIR=<tmp>`` so parallel
    test runs can't step on each other and the repo state stays clean.
    """
    work = tmp_path / "blue-green"
    work.mkdir()
    for name in ("active_color", "upstream-blue.caddy", "upstream-green.caddy"):
        shutil.copy2(STATE_DIR / name, work / name)
    # Recreate the symlink (shutil.copy2 on a symlink follows it by default
    # on some platforms, so be explicit here).
    target = (STATE_DIR / "active_upstream.caddy").resolve().name
    (work / "active_upstream.caddy").symlink_to("upstream-blue.caddy")
    # Sanity: make sure the fresh sandbox starts in a known state.
    assert (work / "active_color").read_text().strip() == "blue"
    return work


def _run_switch(
    *args: str, sandbox: Path, timeout: int = 15
) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["OMNISIGHT_BLUEGREEN_DIR"] = str(sandbox)
    return subprocess.run(
        ["bash", str(SWITCH_SH), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# (1) State directory — physical files present and well-shaped
# ---------------------------------------------------------------------------


class TestStateDirectoryShape:
    def test_state_directory_exists(self) -> None:
        assert STATE_DIR.is_dir(), (
            f"blue-green state directory missing: {STATE_DIR}. "
            "Row 1354 requires deploy/blue-green/ with active_color, the "
            "two upstream-<color>.caddy snippets, and an active_upstream.caddy "
            "symlink."
        )

    def test_active_color_file_exists_and_is_blue_or_green(self) -> None:
        # Source-of-record plain file. Must be a real regular file (not
        # a symlink) so that a `readlink` on it doesn't chain back to
        # a different color's file.
        assert ACTIVE_COLOR_FILE.is_file(), (
            f"active_color file missing at {ACTIVE_COLOR_FILE}"
        )
        assert not ACTIVE_COLOR_FILE.is_symlink()
        content = ACTIVE_COLOR_FILE.read_text().strip()
        assert content in ("blue", "green"), (
            f"active_color content must be 'blue' or 'green' (got {content!r})"
        )

    def test_blue_snippet_exists_and_targets_backend_a(self) -> None:
        assert BLUE_SNIPPET.is_file()
        text = BLUE_SNIPPET.read_text()
        # Shared snippet name — the consumer Caddyfile imports by name,
        # so blue and green MUST expose identical names so swapping the
        # symlink is enough (no Caddyfile edit required).
        assert "(active_upstream_rp)" in text, (
            "blue snippet must declare named snippet `(active_upstream_rp)` "
            "so green/blue are symmetric at the import site"
        )
        assert "backend-a:8000" in text, (
            "blue snippet must route to backend-a:8000 by default"
        )
        assert "OMNISIGHT_UPSTREAM_A" in text, (
            "blue snippet must honour the OMNISIGHT_UPSTREAM_A env override "
            "(consistent with deploy/reverse-proxy/Caddyfile)"
        )

    def test_green_snippet_exists_and_targets_backend_b(self) -> None:
        assert GREEN_SNIPPET.is_file()
        text = GREEN_SNIPPET.read_text()
        assert "(active_upstream_rp)" in text, (
            "green snippet must declare named snippet `(active_upstream_rp)` "
            "so green/blue are symmetric at the import site"
        )
        assert "backend-b:8001" in text, (
            "green snippet must route to backend-b:8001 by default"
        )
        assert "OMNISIGHT_UPSTREAM_B" in text

    def test_active_upstream_is_a_symlink(self) -> None:
        # Symlink, not a regular file — that's the whole atomicity
        # contract: rename(2) over a symlink is atomic.
        assert ACTIVE_SYMLINK.is_symlink(), (
            f"active_upstream.caddy must be a SYMLINK (not a regular file). "
            f"Current: {ACTIVE_SYMLINK} → {os.readlink(ACTIVE_SYMLINK) if ACTIVE_SYMLINK.exists() else '(missing)'}"
        )

    def test_active_symlink_target_matches_active_color(self) -> None:
        # Committed state must be internally consistent: if active_color
        # says "blue", the symlink must point at upstream-blue.caddy.
        target = os.readlink(ACTIVE_SYMLINK)
        color = ACTIVE_COLOR_FILE.read_text().strip()
        assert target == f"upstream-{color}.caddy", (
            f"symlink target {target!r} does not match active_color {color!r}"
        )

    def test_symlink_target_is_relative(self) -> None:
        # Relative symlink keeps the deploy dir portable (can be mounted
        # at any path inside a container). Absolute symlinks break
        # portability and would leak the build-host path into the repo.
        target = os.readlink(ACTIVE_SYMLINK)
        assert not target.startswith("/"), (
            f"active_upstream.caddy symlink must be RELATIVE (got {target!r}); "
            "absolute targets break docker volume mounts and CI caching"
        )


# ---------------------------------------------------------------------------
# (2) bluegreen_switch.sh — file hygiene
# ---------------------------------------------------------------------------


class TestSwitchScriptHygiene:
    def test_switch_sh_exists(self) -> None:
        assert SWITCH_SH.is_file()

    def test_switch_sh_is_executable(self) -> None:
        mode = SWITCH_SH.stat().st_mode
        assert mode & stat.S_IXUSR, (
            "scripts/bluegreen_switch.sh must be executable — operators "
            "invoke it directly in runbooks"
        )

    def test_switch_sh_uses_strict_mode(self, switch_sh_text: str) -> None:
        # `set -euo pipefail` is non-negotiable for anything that
        # performs atomic state changes — a silent substitution bug
        # would leave the symlink dangling or the state file empty.
        assert "set -euo pipefail" in switch_sh_text

    def test_switch_sh_bash_shebang(self, switch_sh_text: str) -> None:
        first = switch_sh_text.splitlines()[0]
        assert first.startswith("#!") and "bash" in first, (
            "switch script needs bash (arrays, [[, local) — POSIX sh insufficient"
        )

    def test_switch_sh_bash_syntax_valid(self) -> None:
        # `bash -n` parses without executing; catches fat-finger bugs.
        result = subprocess.run(
            ["bash", "-n", str(SWITCH_SH)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, (
            f"bluegreen_switch.sh has a shell syntax error:\n{result.stderr}"
        )


# ---------------------------------------------------------------------------
# (3) Atomic primitives — the rename(2) pattern is actually used
# ---------------------------------------------------------------------------


class TestAtomicPrimitives:
    def test_uses_mv_dash_T_for_symlink_swap(self, switch_sh_text: str) -> None:
        # `mv -Tf` is the atomic-replace-over-symlink primitive. Without
        # `-T` (no-target-directory), mv would move the tmp file INTO
        # the target if the target is ever accidentally a directory —
        # silent bug, no atomicity.
        assert re.search(r"mv\s+-Tf\s+", switch_sh_text), (
            "relink_atomic must use `mv -Tf` for the symlink swap "
            "(rename(2) is the only atomic option; -T prevents the "
            "move-into-dir footgun)"
        )

    def test_uses_tmp_then_mv_pattern_for_state_file(
        self, switch_sh_text: str
    ) -> None:
        # Plain state-file writes must use the same pattern — create a
        # tmp next to the target, then rename into place. A naive
        # `printf > active_color` truncates and re-writes, which is
        # NOT atomic (a reader can see an empty file mid-write).
        #
        # The regex is generous: we're really checking for the
        # combination of (a) a `.tmp.$$` path name and (b) a `mv -f`
        # onto the real state file path, anywhere in the script. That
        # lets the implementation use a local tmp variable for
        # readability without this test pinning the exact identifier.
        assert re.search(r"\.tmp\.\$\$", switch_sh_text), (
            "state writes must use a `.tmp.$$` sibling path (PID-suffixed "
            "to avoid collisions across concurrent switch invocations)"
        )
        assert re.search(
            r'mv\s+-f\s+"?\$\{?tmp\}?"?\s+"?\$ACTIVE_COLOR_FILE"?', switch_sh_text
        ), (
            "active_color must be updated via `mv -f <tmp> $ACTIVE_COLOR_FILE` "
            "(atomic rename, never truncate-in-place)"
        )
        assert re.search(
            r'mv\s+-f\s+"?\$\{?tmp\}?"?\s+"?\$PREVIOUS_COLOR_FILE"?',
            switch_sh_text,
        ), (
            "previous_color must be updated via `mv -f <tmp> $PREVIOUS_COLOR_FILE`"
        )

    def test_creates_symlink_at_tmp_path_before_rename(
        self, switch_sh_text: str
    ) -> None:
        # The canonical atomic symlink-replace idiom: `ln -s target tmp`
        # + `mv -Tf tmp final`. Verifying the `ln -s` at a .tmp path
        # pins that we're NOT using the naive `ln -sfn` which on some
        # filesystems is a non-atomic unlink+symlink (visible window).
        assert re.search(r'ln\s+-s\s+"?\$\{?target_basename\}?"?\s+"?\$\{?tmp', switch_sh_text) \
            or re.search(r'ln\s+-s\s+"\$target_basename"\s+"\$tmp"?', switch_sh_text), (
            "relink_atomic must create the new symlink at a tmp path first "
            "(not `ln -sfn` over the live path — that's a two-syscall "
            "unlink+symlink with an observable missing-file window)"
        )


# ---------------------------------------------------------------------------
# (4) Runtime behaviour — exercise the subcommands end-to-end
# ---------------------------------------------------------------------------


class TestRuntimeBehaviour:
    def test_no_args_prints_usage_and_exits_nonzero(self, sandbox: Path) -> None:
        result = _run_switch(sandbox=sandbox)
        assert result.returncode != 0
        assert "usage:" in result.stderr.lower()

    def test_status_prints_all_expected_fields(self, sandbox: Path) -> None:
        result = _run_switch("status", sandbox=sandbox)
        assert result.returncode == 0, result.stderr
        out = result.stdout
        for field in ("active=", "standby=", "symlink_target=", "symlink_color=", "previous="):
            assert field in out, f"status output missing {field!r}:\n{out}"
        assert "active=blue" in out
        assert "standby=green" in out

    def test_switch_flips_color_and_records_previous(self, sandbox: Path) -> None:
        before_color = (sandbox / "active_color").read_text().strip()
        assert before_color == "blue"

        result = _run_switch("switch", sandbox=sandbox)
        assert result.returncode == 0, result.stderr

        after_color = (sandbox / "active_color").read_text().strip()
        assert after_color == "green", "switch must flip blue→green"
        assert os.readlink(sandbox / "active_upstream.caddy") == "upstream-green.caddy"
        assert (sandbox / "previous_color").read_text().strip() == "blue", (
            "previous_color must record the outgoing color for rollback"
        )

    def test_switch_is_atomic_end_state_consistent(self, sandbox: Path) -> None:
        # After a successful switch, the state file and symlink must
        # agree. No half-written intermediate is visible to an outside
        # observer by the time the script returns.
        result = _run_switch("switch", sandbox=sandbox)
        assert result.returncode == 0
        color_file = (sandbox / "active_color").read_text().strip()
        target = os.readlink(sandbox / "active_upstream.caddy")
        assert target == f"upstream-{color_file}.caddy", (
            f"post-switch inconsistency: state={color_file}, symlink={target}"
        )

    def test_no_tmp_files_left_behind_after_switch(self, sandbox: Path) -> None:
        # The .tmp.<pid> files are an implementation detail of the
        # atomic dance — they must never survive a successful run.
        # A leftover tmp means the `mv` step didn't execute and the
        # state is half-written.
        result = _run_switch("switch", sandbox=sandbox)
        assert result.returncode == 0
        stragglers = [p.name for p in sandbox.iterdir() if ".tmp." in p.name]
        assert not stragglers, f"tmp files must be cleaned up; found {stragglers!r}"

    def test_set_active_same_color_is_idempotent(self, sandbox: Path) -> None:
        # `set-active blue` when already blue must be a no-op: no
        # previous_color write, no state change, exit 0. Idempotency
        # matters because the deploy.sh row-1355 ceremony will call
        # set-active on the standby color, and retrying after a partial
        # failure shouldn't clobber rollback state.
        result = _run_switch("set-active", "blue", sandbox=sandbox)
        assert result.returncode == 0, result.stderr
        assert "no-op" in (result.stdout + result.stderr).lower()
        assert not (sandbox / "previous_color").exists(), (
            "idempotent no-op must NOT write previous_color (would clobber "
            "the rollback breadcrumb from a prior real switch)"
        )

    def test_set_active_invalid_color_rejected(self, sandbox: Path) -> None:
        result = _run_switch("set-active", "red", sandbox=sandbox)
        assert result.returncode == 1
        assert "invalid color" in result.stderr.lower()

    def test_set_active_no_arg_rejected(self, sandbox: Path) -> None:
        result = _run_switch("set-active", sandbox=sandbox)
        assert result.returncode == 1
        # Usage hint or "requires" or "invalid" — any of these signals
        # a deliberate reject vs. silent pass-through.
        err = result.stderr.lower()
        assert any(s in err for s in ("usage", "invalid", "required")), (
            f"missing-arg reject must produce a helpful error; got {result.stderr!r}"
        )

    def test_rollback_flips_to_previous_color(self, sandbox: Path) -> None:
        # switch (blue → green) → rollback → expect blue.
        r1 = _run_switch("switch", sandbox=sandbox)
        assert r1.returncode == 0
        assert (sandbox / "active_color").read_text().strip() == "green"

        r2 = _run_switch("rollback", sandbox=sandbox)
        assert r2.returncode == 0, r2.stderr
        assert (sandbox / "active_color").read_text().strip() == "blue"
        assert os.readlink(sandbox / "active_upstream.caddy") == "upstream-blue.caddy"

    def test_rollback_without_previous_state_fails(self, sandbox: Path) -> None:
        # Fresh sandbox has no previous_color. Rollback must refuse
        # rather than default to (e.g.) the other color — defaulting
        # would mask a missing state bug.
        result = _run_switch("rollback", sandbox=sandbox)
        assert result.returncode != 0
        assert "previous" in result.stderr.lower()

    def test_unknown_subcommand_rejected(self, sandbox: Path) -> None:
        result = _run_switch("teleport", sandbox=sandbox)
        assert result.returncode != 0
        assert "unknown subcommand" in result.stderr.lower() or "usage" in result.stderr.lower()

    def test_double_switch_returns_to_origin(self, sandbox: Path) -> None:
        # Two switches in a row must land back at blue — sanity check
        # that "switch" really is `other_color(current)` and not a
        # one-way door.
        assert _run_switch("switch", sandbox=sandbox).returncode == 0
        assert _run_switch("switch", sandbox=sandbox).returncode == 0
        assert (sandbox / "active_color").read_text().strip() == "blue"
        assert os.readlink(sandbox / "active_upstream.caddy") == "upstream-blue.caddy"


# ---------------------------------------------------------------------------
# (5) Crash-consistency — detect state/symlink mismatch on status
# ---------------------------------------------------------------------------


class TestCrashConsistency:
    def test_status_warns_on_state_symlink_mismatch(self, sandbox: Path) -> None:
        # Simulate a crash between step (2) relink and step (3) state
        # write: fix the symlink to point at green but leave the state
        # file saying blue. `status` must surface the mismatch to stderr
        # so an operator can reconcile — silently returning success
        # would hide a real invariant violation.
        (sandbox / "active_upstream.caddy").unlink()
        (sandbox / "active_upstream.caddy").symlink_to("upstream-green.caddy")
        # active_color still says "blue"

        result = _run_switch("status", sandbox=sandbox)
        # status itself should still succeed (it's a read-only command),
        # but it must warn on stderr.
        assert result.returncode == 0
        assert "mismatch" in result.stderr.lower() or "mismatch" in result.stdout.lower(), (
            f"status must flag state/symlink mismatch; stderr={result.stderr!r}"
        )

    def test_reconcile_via_set_active_on_mismatch(self, sandbox: Path) -> None:
        # Induce the mismatch, then run `set-active <matching>` — the
        # script must re-link to reconcile even though the state file
        # already "agrees" with the requested color.
        (sandbox / "active_upstream.caddy").unlink()
        (sandbox / "active_upstream.caddy").symlink_to("upstream-green.caddy")
        # active_color still says "blue"; caller asks for blue.

        result = _run_switch("set-active", "blue", sandbox=sandbox)
        assert result.returncode == 0, result.stderr
        # Post-reconcile: symlink must now point at blue again.
        assert os.readlink(sandbox / "active_upstream.caddy") == "upstream-blue.caddy"


# ---------------------------------------------------------------------------
# (6) Integration — deploy.sh surfaces the new primitive
# ---------------------------------------------------------------------------


class TestDeployShIntegration:
    def test_deploy_sh_references_switch_script(self, deploy_sh_text: str) -> None:
        # The blue-green dispatch arm in deploy.sh must know where the
        # atomic switch primitive lives — both so it can print status
        # (row 1354 visibility) and so the runbook/stderr message can
        # direct operators to invoke it manually today.
        assert "scripts/bluegreen_switch.sh" in deploy_sh_text, (
            "deploy.sh blue-green arm must reference scripts/bluegreen_switch.sh "
            "so row 1354's primitive is discoverable to operators"
        )

    def test_deploy_sh_still_exits_5_on_blue_green(self, deploy_sh_text: str) -> None:
        # Row 1354 delivers the switch mechanism but NOT the full
        # ceremony. Deploy.sh must still fail closed (exit 5) until
        # rows 1355-1357 land — regression lock against a well-meaning
        # refactor that wires the switch call prematurely without the
        # pre-cut smoke gate.
        match = re.search(
            r'if\s+\[\[\s+"\$STRATEGY"\s*==\s*"blue-green"\s*\]\];\s*then(.+?)elif\s+\[\[\s+"\$STRATEGY"\s*==\s*"rolling"',
            deploy_sh_text,
            flags=re.DOTALL,
        )
        assert match, "could not locate blue-green dispatch body"
        body = match.group(1)
        assert re.search(r"\bexit\s+5\b", body), (
            "deploy.sh blue-green arm must still `exit 5` — full "
            "ceremony (pre-cut smoke → switch → observe) is rows 1355-1357"
        )

    def test_deploy_sh_blue_green_body_references_state_dir(
        self, deploy_sh_text: str
    ) -> None:
        # Operator reading the stderr should see the state dir so they
        # can `ls deploy/blue-green/` and inspect state without grepping
        # the whole repo.
        match = re.search(
            r'if\s+\[\[\s+"\$STRATEGY"\s*==\s*"blue-green"\s*\]\];\s*then(.+?)elif\s+\[\[',
            deploy_sh_text,
            flags=re.DOTALL,
        )
        assert match
        body = match.group(1)
        assert "deploy/blue-green" in body, (
            "deploy.sh blue-green body must mention deploy/blue-green/ so "
            "operators can locate the state dir from the error message alone"
        )
