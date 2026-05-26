"""OP-1741 — advance the release-SHA-pinned prod checkout on deploy.

Background
----------
OP-1717 moved the prod compose stack onto a dedicated, release-SHA-pinned
checkout (``/home/user/omnisight-prod``). OP-1741 closes the remaining gap:
a prod deploy must ADVANCE that pin to the new release SHA so the compose
context (``docker-compose.prod.yml`` + ``scripts/``) matches the deployed
release — without the operator hand-running ``git checkout`` and without
ever deploying from a throwaway ``/tmp`` worktree.

Two units carry the contract:

* ``scripts/advance_prod_checkout.sh`` — ``git fetch`` + ``git checkout
  --detach <sha>``, fail-closed on a dirty tree, plus a canonical-checkout
  assertion (errs on a ``/tmp`` / linked worktree).
* ``scripts/deploy-prod.sh`` — gained ``--release-sha`` (delegates to the
  helper) and a canonical-checkout assertion on every deploy.

These tests drive the helper over real, hermetic temp git repos (no network
/ docker), exercising both AC paths: the checkout-advance and the
fail-closed-on-dirty abort. The ``OMNISIGHT_PROD_CHECKOUT`` override lets a
temp repo stand in as "the canonical pinned checkout".
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ADVANCE_SH = REPO_ROOT / "scripts" / "advance_prod_checkout.sh"
DEPLOY_SH = REPO_ROOT / "scripts" / "deploy-prod.sh"


def _git(repo: Path, *args: str) -> str:
    """Run a git command in ``repo`` with a hermetic identity, return stdout."""
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "op1741",
        "GIT_AUTHOR_EMAIL": "op1741@example.invalid",
        "GIT_COMMITTER_NAME": "op1741",
        "GIT_COMMITTER_EMAIL": "op1741@example.invalid",
    }
    out = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, env=env, check=True,
    )
    return out.stdout.strip()


def _make_prod_checkout(tmp_path: Path) -> tuple[Path, str, str]:
    """A standin canonical prod checkout with an ``origin`` remote.

    Builds: a bare ``origin`` repo with commits c1 (sha1) and c2 (sha2) on
    ``main``, and a working checkout sitting at c1 (so an advance to sha2 is
    a real move). Returns ``(checkout_path, sha1, sha2)``.
    """
    origin = tmp_path / "origin.git"
    checkout = tmp_path / "omnisight-prod"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    _git(checkout, "config", "commit.gpgsign", "false")

    (checkout / "docker-compose.prod.yml").write_text("# c1\n", encoding="utf-8")
    _git(checkout, "add", "-A")
    _git(checkout, "commit", "-qm", "c1")
    sha1 = _git(checkout, "rev-parse", "HEAD")
    _git(checkout, "remote", "add", "origin", str(origin))
    _git(checkout, "push", "-q", "origin", "HEAD:refs/heads/main")

    (checkout / "docker-compose.prod.yml").write_text("# c2\n", encoding="utf-8")
    _git(checkout, "commit", "-qam", "c2")
    sha2 = _git(checkout, "rev-parse", "HEAD")
    _git(checkout, "push", "-q", "origin", "HEAD:refs/heads/main")

    # Sit the checkout back on c1 so advancing to sha2 is an observable move.
    _git(checkout, "checkout", "-q", sha1)
    return checkout, sha1, sha2


def _run(checkout: Path, *args: str) -> subprocess.CompletedProcess:
    """Run advance_prod_checkout.sh from ``checkout``, declaring it canonical."""
    env = {**os.environ, "OMNISIGHT_PROD_CHECKOUT": str(checkout)}
    return subprocess.run(
        ["bash", str(ADVANCE_SH), *args],
        capture_output=True, text=True, cwd=str(checkout), env=env,
    )


# ═════════════════════════════════════════════════════════════════════
# AC "Exercised" — the checkout-advance path
# ═════════════════════════════════════════════════════════════════════


def test_advance_moves_pin_to_release_sha(tmp_path: Path) -> None:
    """A clean checkout at c1 advances (fetch+checkout) to the c2 SHA."""
    checkout, sha1, sha2 = _make_prod_checkout(tmp_path)
    assert _git(checkout, "rev-parse", "HEAD") == sha1  # precondition

    proc = _run(checkout, f"--release-sha={sha2}")
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "advanced to" in out
    # The pin actually moved, and the compose context now matches the release.
    assert _git(checkout, "rev-parse", "HEAD") == sha2
    assert (checkout / "docker-compose.prod.yml").read_text() == "# c2\n"


def test_advance_dry_run_does_not_move_pin(tmp_path: Path) -> None:
    """--dry-run prints the fetch+checkout plan but leaves HEAD where it was."""
    checkout, sha1, sha2 = _make_prod_checkout(tmp_path)
    proc = _run(checkout, f"--release-sha={sha2}", "--dry-run")
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "[dry-run]" in out and "git checkout" in out
    assert _git(checkout, "rev-parse", "HEAD") == sha1  # unchanged


def test_advance_rejects_unknown_sha(tmp_path: Path) -> None:
    """A SHA that does not resolve to a commit aborts (pin not moved)."""
    checkout, sha1, _sha2 = _make_prod_checkout(tmp_path)
    proc = _run(checkout, "--release-sha=" + "0" * 40)
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert _git(checkout, "rev-parse", "HEAD") == sha1


# ═════════════════════════════════════════════════════════════════════
# AC "Exercised" — fail-closed on a dirty checkout
# ═════════════════════════════════════════════════════════════════════


def test_dirty_checkout_aborts_fail_closed(tmp_path: Path) -> None:
    """A dirty working tree aborts the advance (fail-closed) — HEAD unchanged."""
    checkout, sha1, sha2 = _make_prod_checkout(tmp_path)
    (checkout / "docker-compose.prod.yml").write_text("# locally edited\n", encoding="utf-8")

    proc = _run(checkout, f"--release-sha={sha2}")
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, out
    assert "dirty" in out and "fail-closed" in out
    # The pin must NOT have moved off c1.
    assert _git(checkout, "rev-parse", "HEAD") == sha1


def test_dirty_checkout_aborts_assert_only(tmp_path: Path) -> None:
    """--assert-only also fails closed on a dirty tree (no SHA needed)."""
    checkout, _sha1, _sha2 = _make_prod_checkout(tmp_path)
    (checkout / "untracked.txt").write_text("x\n", encoding="utf-8")
    proc = _run(checkout, "--assert-only")
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, out
    assert "dirty" in out


# ═════════════════════════════════════════════════════════════════════
# Canonical-checkout assertion — throwaway worktree rejected, non-canonical warns
# ═════════════════════════════════════════════════════════════════════


def test_linked_worktree_is_rejected(tmp_path: Path) -> None:
    """A throwaway `git worktree add` checkout is rejected (fail-closed) —
    deploys MUST run from the canonical pinned checkout, not a worktree."""
    checkout, _sha1, sha2 = _make_prod_checkout(tmp_path)
    worktree = tmp_path / "throwaway-wt"
    _git(checkout, "worktree", "add", "--detach", str(worktree), sha2)
    # Declare the worktree path itself as "canonical" to prove the rejection
    # is driven by the linked-worktree git-dir, not merely a path mismatch.
    env = {**os.environ, "OMNISIGHT_PROD_CHECKOUT": str(worktree)}
    proc = subprocess.run(
        ["bash", str(ADVANCE_SH), "--assert-only"],
        capture_output=True, text=True, cwd=str(worktree), env=env,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, out
    assert "linked worktree" in out


def test_assert_only_passes_on_clean_canonical_checkout(tmp_path: Path) -> None:
    """The happy path: a clean tree declared as canonical passes the assertion."""
    checkout, _sha1, _sha2 = _make_prod_checkout(tmp_path)
    proc = _run(checkout, "--assert-only")
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "assertion passed" in out


def test_noncanonical_clean_checkout_warns_but_passes(tmp_path: Path) -> None:
    """A clean tree whose path is NOT the canonical one warns and proceeds —
    a plain clone (e.g. a runner workspace) is advisory, not fatal; only a
    throwaway linked worktree or a dirty tree fails closed."""
    checkout, _sha1, _sha2 = _make_prod_checkout(tmp_path)
    env = {**os.environ, "OMNISIGHT_PROD_CHECKOUT": "/home/user/omnisight-prod"}
    proc = subprocess.run(
        ["bash", str(ADVANCE_SH), "--assert-only"],
        capture_output=True, text=True, cwd=str(checkout), env=env,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "not the canonical pinned prod checkout" in out


# ═════════════════════════════════════════════════════════════════════
# AC "Integration" — deploy-prod.sh advances the pin before the stack
# ═════════════════════════════════════════════════════════════════════


def _deploy_sandbox(tmp_path: Path) -> Path:
    """A clean git checkout carrying deploy-prod.sh + its helpers, so a
    --release-sha dry-run walks Step 0 (advance) → the digest deploy."""
    checkout = tmp_path / "omnisight-prod"
    (checkout / "scripts").mkdir(parents=True)
    for name in ("deploy-prod.sh", "advance_prod_checkout.sh", "check_deploy_ref.sh"):
        dst = checkout / "scripts" / name
        dst.write_bytes((REPO_ROOT / "scripts" / name).read_bytes())
        dst.chmod(0o755)
    (checkout / ".env").write_text("", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    _git(checkout, "config", "commit.gpgsign", "false")
    _git(checkout, "add", "-A")
    _git(checkout, "commit", "-qm", "init")
    return checkout


def test_deploy_prod_release_sha_advances_before_deploy(tmp_path: Path) -> None:
    """`deploy-prod.sh --release-sha=<sha> ... --dry-run` runs the Step 0
    checkout-advance BEFORE the digest deploy — and the SHA does not displace
    the digest as the deploy identity (digest-deploy contract unchanged)."""
    checkout = _deploy_sandbox(tmp_path)
    sha = _git(checkout, "rev-parse", "HEAD")
    env = {
        **os.environ,
        "OMNISIGHT_PROD_CHECKOUT": str(checkout),
        "OMNISIGHT_REGISTRY": "reg.example/ns",
    }
    proc = subprocess.run(
        ["bash", str(checkout / "scripts" / "deploy-prod.sh"),
         f"--release-sha={sha}",
         f"--backend-digest=sha256:{'a' * 64}",
         f"--frontend-digest=sha256:{'b' * 64}",
         "--dry-run"],
        capture_output=True, text=True, cwd=str(checkout), env=env,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    # Step 0 (advance) appears, and it is ordered BEFORE the digest-deploy step.
    assert "Advance pinned prod checkout" in out
    assert "advance_prod_checkout.sh" in out
    assert out.index("Advance pinned prod checkout") < out.index("Digest deploy")
    # The deploy identity is still the image digests — the SHA only moves the pin.
    assert "backend@sha256:" + "a" * 64 in out


# ═════════════════════════════════════════════════════════════════════
# Static guards — scripts carry the OP-1741 contract
# ═════════════════════════════════════════════════════════════════════


def test_scripts_are_valid_bash() -> None:
    for script in (ADVANCE_SH, DEPLOY_SH):
        rc = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        assert rc.returncode == 0, f"{script}: {rc.stderr}"


def test_advance_helper_is_executable() -> None:
    assert os.access(ADVANCE_SH, os.X_OK), "advance_prod_checkout.sh must be executable"


def test_deploy_prod_wires_release_sha_and_assertion() -> None:
    """deploy-prod.sh exposes --release-sha and gates on the canonical checkout."""
    body = DEPLOY_SH.read_text(encoding="utf-8")
    assert "--release-sha=*" in body
    assert "advance_prod_checkout.sh" in body
    assert "_assert_canonical_checkout" in body
    # The SHA advances the compose pin only — NOT the digest deploy identity.
    assert "PROD_CHECKOUT" in body
