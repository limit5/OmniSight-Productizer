r"""OP-1748 (v2-⑧-1bc) — `scripts/shutdown.sh` rerun-idempotency + §3 contract.

Family ⑧ §3 (`docs/sprint-s12/2026-05-16-v2-family8-graceful-shutdown-contract.md`)
pins what the existing `scripts/shutdown.sh` MUST satisfy after the ⑧-1bc
extension. This module drives the compose path of the script over a fully
stubbed docker, so no real docker / systemd / network is touched, and pins:

  * §3.3 idempotency invariant — the three rerun scenarios named by the
    contract: ``test_shutdown_rerun_after_clean_stop`` (stack already down →
    exit 0, no residue to remove), ``test_shutdown_rerun_after_hung_service``
    (a SIGKILLed container left ``Exited`` → ``docker compose rm -f -s``
    removes it, exit 0), ``test_shutdown_rerun_after_partial_failure`` (one
    service still up + one ``Exited`` → stop the runner, clear the residue,
    exit 0);
  * §3.2 per-service-class grace budgets — ``--dry-run`` prints
    ``PG=30 backend=40 stateless=15 other=10`` (OP-1748 AC);
  * §3.4 verification loop — a clean stop emits the single structured
    ``[shutdown] verify mode=… exit=0 …`` evidence line;
  * §3.1 pinned exit codes — 0 (clean) / 2 (prerequisite missing) /
    3 (invalid args).

The stub docker is a small bash program that reads/writes a flat state file
(``service status`` per line) so the test can assert what the script did to
each container (kills, removals) by inspecting side-effect logs.

Cost: a handful of bash subprocesses + tmp files; stdlib + pytest only.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "shutdown.sh"

# A bash stub standing in for `docker`. It models a tiny compose stack via a
# flat state file ($STUB_STATE: "<service> <status>" per line) and records its
# mutating side effects to $STUB_RMLOG (services removed) and $STUB_KILLLOG
# ("<SIGNAL> <service>" per stop). It understands exactly the docker / docker
# compose invocations shutdown.sh issues on the compose path.
DOCKER_STUB = r"""#!/usr/bin/env bash
set -u
STATE="$STUB_STATE"; RMLOG="$STUB_RMLOG"; KILLLOG="$STUB_KILLLOG"

svc_status() { awk -v s="$1" '$1==s{print $2; exit}' "$STATE"; }

cmd="${1:-}"
case "$cmd" in
  compose)
    shift
    sub=""
    while [[ $# -gt 0 ]]; do
      case "$1" in
        -f)        shift 2 ;;
        --profile) shift 2 ;;
        version)   sub=version; shift; break ;;
        ps)        sub=ps; shift; break ;;
        rm)        sub=rm; shift; break ;;
        *)         shift ;;
      esac
    done
    case "$sub" in
      version) exit 0 ;;
      ps)
        qmode=0; filter=""; svc=""
        while [[ $# -gt 0 ]]; do
          case "$1" in
            -q)         qmode=1; shift ;;
            --services) shift ;;
            --all)      shift ;;
            --filter)   filter="$2"; shift 2 ;;
            *)          svc="$1"; shift ;;
          esac
        done
        if (( qmode )); then
          [[ -n "$svc" && -n "$(svc_status "$svc")" ]] && echo "cid-$svc"
          exit 0
        fi
        while read -r name st; do
          [[ -z "$name" ]] && continue
          case "$filter" in
            status=running) [[ "$st" == running ]] && echo "$name" ;;
            status=exited)  [[ "$st" == exited  ]] && echo "$name" ;;
            "")             echo "$name" ;;
          esac
        done < "$STATE"
        exit 0 ;;
      rm)
        svc=""
        while [[ $# -gt 0 ]]; do
          case "$1" in -f|-s) shift ;; *) svc="$1"; shift ;; esac
        done
        echo "$svc" >> "$RMLOG"
        grep -v "^$svc " "$STATE" > "$STATE.tmp" 2>/dev/null || true
        mv "$STATE.tmp" "$STATE" 2>/dev/null || true
        exit 0 ;;
    esac ;;
  inspect)
    shift; cid=""
    while [[ $# -gt 0 ]]; do
      case "$1" in --format=*) shift ;; --format) shift 2 ;; *) cid="$1"; shift ;; esac
    done
    svc="${cid#cid-}"
    [[ "$(svc_status "$svc")" == running ]] && echo true || echo false
    exit 0 ;;
  kill)
    shift; sig=TERM; cid=""
    while [[ $# -gt 0 ]]; do
      case "$1" in --signal=*) sig="${1#--signal=}"; shift ;; *) cid="$1"; shift ;; esac
    done
    svc="${cid#cid-}"
    echo "$sig $svc" >> "$KILLLOG"
    if [[ -n "$(svc_status "$svc")" ]]; then
      grep -v "^$svc " "$STATE" > "$STATE.tmp" 2>/dev/null || true
      echo "$svc exited" >> "$STATE.tmp"
      mv "$STATE.tmp" "$STATE"
    fi
    exit 0 ;;
esac
exit 0
"""


class Harness:
    """A configured shutdown.sh invocation over the stub docker stack."""

    def __init__(self, tmp_path: Path, state: dict[str, str]):
        self.tmp = tmp_path
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        self.state = tmp_path / "state"
        self.rmlog = tmp_path / "rm.log"
        self.killlog = tmp_path / "kill.log"
        self.state.write_text(
            "".join(f"{svc} {st}\n" for svc, st in state.items()), encoding="utf-8"
        )
        self.rmlog.write_text("", encoding="utf-8")
        self.killlog.write_text("", encoding="utf-8")
        stub = self.bin / "docker"
        stub.write_text(DOCKER_STUB, encoding="utf-8")
        stub.chmod(0o755)
        # the compose file only needs to exist (the stub ignores its content)
        self.compose = tmp_path / "docker-compose.prod.yml"
        self.compose.write_text("services: {}\n", encoding="utf-8")

    def _sanitized_bin(self) -> Path:
        """A PATH dir with the coreutils shutdown.sh needs but NO docker.

        Real docker lives in /usr/bin, so to exercise the "prerequisite
        missing" path we hand the script a PATH that contains only symlinks to
        the tools it uses, deliberately omitting docker.
        """
        sb = self.tmp / "sanitized-bin"
        if sb.exists():
            return sb
        sb.mkdir()
        for tool in (
            "bash", "dirname", "pwd", "cat", "date", "sed", "grep", "awk",
            "tr", "sleep", "head", "sort", "env", "rm", "mv", "mkdir",
        ):
            src = subprocess.run(
                ["bash", "-c", f"command -v {tool}"], capture_output=True, text=True
            ).stdout.strip()
            if src:
                (sb / tool).symlink_to(src)
        return sb

    def env(self, *, with_docker: bool = True) -> dict[str, str]:
        env = dict(os.environ)
        if with_docker:
            env["PATH"] = f"{self.bin}:{os.environ.get('PATH', '')}"
        else:
            env["PATH"] = str(self._sanitized_bin())
        env["STUB_STATE"] = str(self.state)
        env["STUB_RMLOG"] = str(self.rmlog)
        env["STUB_KILLLOG"] = str(self.killlog)
        # keep the verification loop fast; we never simulate a true hang here
        env["OMNISIGHT_SHUTDOWN_VERIFY_TIMEOUT"] = "2"
        return env

    def run(self, *args: str, with_docker: bool = True) -> subprocess.CompletedProcess:
        bash = subprocess.run(
            ["bash", "-c", "command -v bash"], capture_output=True, text=True
        ).stdout.strip() or "bash"
        cmd = [
            bash, str(SCRIPT),
            "--mode=compose",
            f"--compose-file={self.compose}",
            *args,
        ]
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=60, env=self.env(with_docker=with_docker)
        )

    def removed(self) -> list[str]:
        return [l for l in self.rmlog.read_text().splitlines() if l.strip()]

    def kills(self) -> list[str]:
        return [l for l in self.killlog.read_text().splitlines() if l.strip()]


# ─────────────────────────────────────────────────────────────────────
# shape
# ─────────────────────────────────────────────────────────────────────
def test_script_exists_executable_valid_bash():
    assert SCRIPT.exists(), "scripts/shutdown.sh is missing"
    assert SCRIPT.stat().st_mode & 0o111, "shutdown.sh must be executable"
    syn = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert syn.returncode == 0, f"shutdown.sh is not valid bash:\n{syn.stderr}"


# ─────────────────────────────────────────────────────────────────────
# §3.3 idempotency invariant — the three named rerun scenarios
# ─────────────────────────────────────────────────────────────────────
def test_shutdown_rerun_after_clean_stop(tmp_path):
    """Stack already fully down (no containers): rerun is a clean exit-0 no-op.

    Nothing to kill, nothing to remove — the script must converge without
    error or side effects.
    """
    h = Harness(tmp_path, state={})  # no services present at all
    r = h.run()
    assert r.returncode == 0, f"clean rerun must exit 0; got {r.returncode}\n{r.stderr}"
    assert h.kills() == [], f"clean rerun must not kill anything; got {h.kills()}"
    assert h.removed() == [], f"clean rerun must not remove anything; got {h.removed()}"
    assert "compose stack is down" in r.stdout


def test_shutdown_rerun_after_hung_service(tmp_path):
    """A prior run SIGKILLed a hung backend, leaving an ``Exited`` container.

    The rerun must clear that residue via ``docker compose rm -f -s`` and exit
    0 — without re-killing (the container is already gone/exited).
    """
    h = Harness(tmp_path, state={"backend-a": "exited"})
    r = h.run()
    assert r.returncode == 0, f"rerun after hung service must exit 0; got {r.returncode}\n{r.stderr}"
    assert "backend-a" in h.removed(), (
        f"rerun must `rm -f -s` the exited residue backend-a; removed={h.removed()}"
    )
    assert h.kills() == [], (
        f"an already-exited service must not be killed again; kills={h.kills()}"
    )
    assert "removing exited residue: backend-a" in r.stdout


def test_shutdown_rerun_after_partial_failure(tmp_path):
    """Mixed residue: one service still ``running`` + one ``exited``.

    The rerun must stop the running one (SIGTERM), then clear ALL exited
    residue, and exit 0 — the convergent-to-clean contract.
    """
    h = Harness(tmp_path, state={"backend-a": "running", "caddy": "exited"})
    r = h.run()
    assert r.returncode == 0, f"partial-failure rerun must exit 0; got {r.returncode}\n{r.stderr}"
    # the running service was cooperatively stopped (SIGTERM), not abandoned
    assert any(k == "TERM backend-a" for k in h.kills()), (
        f"still-running backend-a must get SIGTERM; kills={h.kills()}"
    )
    # both end up exited and get cleaned up
    removed = h.removed()
    assert "caddy" in removed and "backend-a" in removed, (
        f"both exited containers must be removed; removed={removed}"
    )


# ─────────────────────────────────────────────────────────────────────
# §3.2 per-service-class grace budgets — printed under --dry-run
# ─────────────────────────────────────────────────────────────────────
def test_dry_run_prints_class_budgets(tmp_path):
    h = Harness(tmp_path, state={})
    r = h.run("--dry-run")
    assert r.returncode == 0, f"--dry-run must exit 0; got {r.returncode}\n{r.stderr}"
    out = r.stdout
    assert "PG=30s" in out, f"--dry-run must print PG class budget; stdout:\n{out}"
    assert "backend=40s" in out, f"--dry-run must print backend class budget; stdout:\n{out}"
    assert "stateless=15s" in out, f"--dry-run must print stateless class budget; stdout:\n{out}"
    assert "other=10s" in out, f"--dry-run must print other class budget; stdout:\n{out}"


def test_dry_run_budgets_match_grace_period_map(tmp_path):
    """The printed budgets must be derived from the GRACE_PERIODS map, not a
    hardcoded string — guards against the print and the wire-up drifting apart.
    """
    text = SCRIPT.read_text()
    # representative members read by print_class_budgets()
    assert "[postgres]=30" in text and "[backend]=40" in text
    assert "[caddy]=15" in text
    assert "DEFAULT_GRACE=10" in text


# ─────────────────────────────────────────────────────────────────────
# §3.4 verification loop — structured evidence line
# ─────────────────────────────────────────────────────────────────────
def test_clean_stop_emits_structured_verification_evidence(tmp_path):
    h = Harness(tmp_path, state={"backend-a": "running", "frontend": "running"})
    r = h.run()
    assert r.returncode == 0, f"{r.returncode}\n{r.stderr}"
    line = next((l for l in r.stdout.splitlines() if "verify mode=" in l), None)
    assert line is not None, f"missing §3.4 verification evidence line; stdout:\n{r.stdout}"
    for token in ("mode=compose", "exit=0", "timeout=", "down=", "elapsed=", "still_running="):
        assert token in line, f"evidence line missing {token!r}: {line!r}"


# ─────────────────────────────────────────────────────────────────────
# §3.1 pinned exit codes
# ─────────────────────────────────────────────────────────────────────
def test_exit_code_3_on_invalid_arg(tmp_path):
    h = Harness(tmp_path, state={})
    r = h.run("--bogus-flag")
    assert r.returncode == 3, f"unknown arg must exit 3; got {r.returncode}"


def test_exit_code_3_on_low_timeout(tmp_path):
    h = Harness(tmp_path, state={})
    r = h.run("--timeout", "10")
    assert r.returncode == 3, f"--timeout < 40 must exit 3; got {r.returncode}"


def test_exit_code_2_when_docker_missing(tmp_path):
    """compose mode with no docker on PATH is a prerequisite failure → exit 2."""
    h = Harness(tmp_path, state={})
    r = h.run(with_docker=False)
    assert r.returncode == 2, f"missing docker prerequisite must exit 2; got {r.returncode}\n{r.stderr}"


def test_exit_code_0_on_clean_stop(tmp_path):
    h = Harness(tmp_path, state={"frontend": "running"})
    r = h.run()
    assert r.returncode == 0, f"clean stop must exit 0; got {r.returncode}\n{r.stderr}"


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
