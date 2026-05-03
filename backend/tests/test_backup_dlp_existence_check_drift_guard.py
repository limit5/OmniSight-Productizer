"""FX.7.10 — drift guard for the DLP-scanner existence preflight in backup_prod_db.sh.

Background
----------
The 2026-05-03 deep audit row FX.7.10 flagged that
``scripts/backup_prod_db.sh`` invoked the DLP scanner as a bare
``python3 scripts/backup_dlp_scan.py`` call: when the scanner script
was missing on the prod host (deploy bundle stripped, file renamed,
scripts/ not mounted, etc.) the existing failure path collapsed
"deploy artefact incomplete" into the same `die` message that a
legitimate plaintext-secret finding produces ("backup DLP scan
failed; plaintext backup shredded"). Two ways that hurts:

* **Diagnostic ambiguity**: operator sees a DLP-blocked message and
  assumes the scanner caught something today, when in fact the
  scanner never ran at all.
* **Silent gate degradation**: if the operator rebuilds the deploy
  bundle without noticing the missing file, the encrypted backup
  artefact ships with *no DLP gate having executed*, while the log
  trail makes it look like DLP "was just blocking things lately".

FX.7.10 layered an explicit existence preflight *before* plaintext
extract: the scanner file must exist and be non-empty (`-s`),
readable (`-r`), and `python3` must be on PATH. The preflight die
message says "deploy artefact incomplete" so the operator triages to
the right place (rebuild image / re-rsync scripts/) instead of
chasing a phantom secret. The actual scan call was rewritten to use
``"$DLP_SCANNER"`` (the validated path) so preflight check and
invocation share one source of truth.

What this test enforces
-----------------------
* Live-tree fact: ``scripts/backup_dlp_scan.py`` actually exists. (If
  this regresses, the FX.7.10 preflight would fire on every prod
  backup run today.)
* The preflight block exists in ``backup_prod_db.sh`` with the canonical
  ``[[ -s "$DLP_SCANNER" ]] || die ...`` form, the readability check,
  the python3 PATH check, and the right die-message wording.
* Ordering: the DLP-scanner existence check appears *before* the
  ``docker compose ... ps`` block (the plaintext extract entry-point).
  The whole point of the preflight is to fail closed without ever
  baking plaintext, so a refactor that moves it after plaintext
  extract silently undoes the security property.
* The actual scan invocation uses ``"$DLP_SCANNER"`` (single source of
  truth) — pre-FX.7.10 the bare ``scripts/backup_dlp_scan.py`` literal
  was used; this guards against a partial revert.
* End-to-end (subprocess): with a stub repo skeleton, gpg/docker
  stubs on PATH, and ``OMNISIGHT_BACKUP_PASSPHRASE`` set:
    - **Scanner missing** → rc≠0, stderr says "DLP scanner missing or
      empty" + "deploy artefact incomplete", and *no plaintext .db is
      ever created* under ``data/backups/``.
    - **Scanner empty (zero bytes)** → same deploy-artefact-incomplete
      die.
    - **Scanner present** → script advances past the DLP preflight
      and dies later at "no live DB found" (proving the gate let it
      through to the next stage rather than blocking).

Why a subprocess e2e instead of a pure-text assertion
-----------------------------------------------------
Bash's ``[[ -s ]]``/``[[ -r ]]`` semantics are easy to subtly break in
a refactor (e.g. quoting ``$DLP_SCANNER`` wrong inside ``[[ ]]`` is
benign, but quoting it wrong in a literal ``test`` would split on
whitespace; using ``-e`` instead of ``-s`` would let an empty file
pass). The contract worth pinning is the actual exit-code + stderr
an operator sees, not the textual form of the conditional.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKUP_SCRIPT = REPO_ROOT / "scripts" / "backup_prod_db.sh"
DLP_SCANNER = REPO_ROOT / "scripts" / "backup_dlp_scan.py"


# ─── Live-tree facts ────────────────────────────────────────────────


def test_dlp_scanner_file_exists_in_repo() -> None:
    """If the scanner is missing in the live tree, the FX.7.10 preflight
    would fire on every prod backup run starting today."""
    assert DLP_SCANNER.exists(), (
        f"FX.7.10 preflight expects {DLP_SCANNER} to exist in the prod "
        f"deploy bundle; it is missing from the repo entirely."
    )
    assert DLP_SCANNER.stat().st_size > 0, (
        f"{DLP_SCANNER} is zero bytes; the preflight `-s` check would "
        f"reject this in production."
    )


def test_backup_script_exists_and_bash_syntax_valid() -> None:
    assert BACKUP_SCRIPT.exists()
    proc = subprocess.run(
        ["bash", "-n", str(BACKUP_SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"bash -n failed: {proc.stderr}"


# ─── Static structure ───────────────────────────────────────────────


def test_preflight_defines_dlp_scanner_variable() -> None:
    text = BACKUP_SCRIPT.read_text()
    assert 'DLP_SCANNER="$REPO/scripts/backup_dlp_scan.py"' in text, (
        "FX.7.10: the preflight must bind DLP_SCANNER once, anchored "
        "to $REPO so it survives `cd` and relative-path drift."
    )


def test_preflight_has_existence_and_readability_checks() -> None:
    text = BACKUP_SCRIPT.read_text()
    assert '[[ -s "$DLP_SCANNER" ]] || \\\n  die ' in text, (
        "FX.7.10: must use `-s` (non-empty existence) not `-e` "
        "(existence only); a zero-byte scanner would still pass `-e`."
    )
    assert '[[ -r "$DLP_SCANNER" ]] || \\\n  die ' in text, (
        "FX.7.10: must check readability so the operator gets a clear "
        "perms-error message instead of a python3 ImportError."
    )
    assert "command -v python3 >/dev/null" in text, (
        "FX.7.10: python3 must be on PATH for the scanner to run."
    )


def test_preflight_die_messages_say_deploy_artefact_incomplete() -> None:
    """The die message wording is the operator's only triage hint —
    pin it so a copy-paste refactor cannot collapse it back into the
    generic "DLP scan failed" message."""
    text = BACKUP_SCRIPT.read_text()
    assert "DLP scanner missing or empty" in text
    assert "deploy artefact incomplete" in text
    assert "aborting BEFORE plaintext extract" in text


def test_preflight_runs_before_plaintext_extract() -> None:
    """The whole security property is "fail closed *before* extracting
    plaintext". If the preflight ever moves below the docker-compose
    ps block (where plaintext gets captured to $PLAIN), the property
    is silently lost."""
    text = BACKUP_SCRIPT.read_text()
    preflight_idx = text.find('DLP_SCANNER="$REPO/scripts/backup_dlp_scan.py"')
    plaintext_capture_idx = text.find("PLAIN=\"$BKP_DIR/${LABEL}-${TS}.db\"")
    docker_ps_idx = text.find("docker compose -f \"$COMPOSE_FILE\" ps --services")
    assert preflight_idx > 0, "DLP_SCANNER assignment not found"
    assert plaintext_capture_idx > 0, "PLAIN assignment not found"
    assert docker_ps_idx > 0, "docker compose ps probe not found"
    assert preflight_idx < plaintext_capture_idx, (
        "FX.7.10 ordering broken: DLP preflight must run BEFORE the "
        "plaintext file path is even constructed"
    )
    assert preflight_idx < docker_ps_idx, (
        "FX.7.10 ordering broken: DLP preflight must run BEFORE the "
        "docker compose live-DB probe (which leads to plaintext extract)"
    )


def test_actual_scan_call_uses_dlp_scanner_variable() -> None:
    """Single source of truth: preflight check and invocation share
    the validated $DLP_SCANNER path. Pre-FX.7.10 the bare literal
    `scripts/backup_dlp_scan.py` was used; this guards against a
    partial revert that re-introduces the literal while keeping the
    preflight in place."""
    text = BACKUP_SCRIPT.read_text()
    assert 'python3 "$DLP_SCANNER" "$PLAIN"' in text
    # The bare-literal form must NOT come back as an executable
    # statement. We ignore comment lines (the FX.7.10 preflight
    # explanatory comment intentionally references the old form to
    # explain why it was replaced).
    code_only = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    assert 'python3 scripts/backup_dlp_scan.py "$PLAIN"' not in code_only


# ─── End-to-end (subprocess) ────────────────────────────────────────


def _build_stub_repo(tmp_path: Path, *, with_scanner: bool, scanner_empty: bool = False) -> tuple[Path, dict[str, str]]:
    """Build a minimal repo skeleton + PATH stubs for gpg/docker/shred.

    Returns (stub_repo, env). The caller invokes the real backup
    script under stub_repo so its `$REPO` resolves to stub_repo
    (the script computes REPO from `BASH_SOURCE`).
    """
    stub_repo = tmp_path / "repo"
    (stub_repo / "scripts").mkdir(parents=True)
    (stub_repo / "data" / "backups").mkdir(parents=True)

    shutil.copy(BACKUP_SCRIPT, stub_repo / "scripts" / "backup_prod_db.sh")
    (stub_repo / "scripts" / "backup_prod_db.sh").chmod(0o755)

    if with_scanner:
        target = stub_repo / "scripts" / "backup_dlp_scan.py"
        if scanner_empty:
            target.write_text("")  # zero-byte file — `-s` should reject
        else:
            shutil.copy(DLP_SCANNER, target)
        target.chmod(0o755)

    # Stub a few external commands on PATH so earlier preflight steps
    # (gpg presence) and later optional steps don't error before our
    # gate fires.
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for cmd in ("gpg", "docker", "shred"):
        stub = fake_bin / cmd
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(0o755)

    env = {
        # Keep python3, bash, basic coreutils accessible from system PATH.
        "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
        "OMNISIGHT_BACKUP_PASSPHRASE": "test-passphrase",
        # Force non-tty so colour codes don't pollute stderr.
        "TERM": "dumb",
    }
    return stub_repo, env


def _run_backup(stub_repo: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(stub_repo / "scripts" / "backup_prod_db.sh")],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        cwd=str(stub_repo),
    )


def test_e2e_missing_scanner_fails_closed_before_plaintext_extract(tmp_path: Path) -> None:
    stub_repo, env = _build_stub_repo(tmp_path, with_scanner=False)

    proc = _run_backup(stub_repo, env)

    assert proc.returncode != 0, (
        f"FX.7.10: missing scanner must abort.\nstdout={proc.stdout}\nstderr={proc.stderr}"
    )
    combined = proc.stdout + proc.stderr
    assert "DLP scanner missing or empty" in combined
    assert "deploy artefact incomplete" in combined
    assert "aborting BEFORE plaintext extract" in combined

    # The preflight runs before the docker-compose ps probe; with our
    # stub `docker` (exit 0, empty stdout) the script would otherwise
    # die at "no live DB found". Make sure the *DLP* die fired, not
    # the live-DB one.
    assert "no live DB found" not in combined, (
        "FX.7.10 ordering regression: preflight should have fired BEFORE "
        "the live-DB probe; the live-DB die means the preflight was "
        "skipped or moved below it."
    )

    # And — most importantly — no plaintext .db artefact should exist.
    leaked = list((stub_repo / "data" / "backups").iterdir())
    assert leaked == [], (
        f"FX.7.10: plaintext leaked despite missing-scanner abort: {leaked}"
    )


def test_e2e_empty_scanner_treated_as_missing(tmp_path: Path) -> None:
    """Zero-byte scanner is a real failure mode (broken mount, truncated
    rsync). `-s` rejects it; `-e` would not."""
    stub_repo, env = _build_stub_repo(tmp_path, with_scanner=True, scanner_empty=True)

    proc = _run_backup(stub_repo, env)

    assert proc.returncode != 0
    combined = proc.stdout + proc.stderr
    assert "DLP scanner missing or empty" in combined
    assert "deploy artefact incomplete" in combined


def test_e2e_present_scanner_advances_past_preflight(tmp_path: Path) -> None:
    """With the scanner present, the preflight passes and the script
    advances to the next stage. Our stub `docker` returns empty service
    list and there is no host DB, so the *expected* die is the
    live-DB one — proving the DLP gate let us through."""
    stub_repo, env = _build_stub_repo(tmp_path, with_scanner=True)

    proc = _run_backup(stub_repo, env)

    assert proc.returncode != 0  # we still error on no live DB; that's fine
    combined = proc.stdout + proc.stderr
    assert "no live DB found" in combined, (
        f"Expected to die at live-DB probe (preflight passed) but got:\n"
        f"stdout={proc.stdout}\nstderr={proc.stderr}"
    )
    assert "DLP scanner missing" not in combined
    assert "deploy artefact incomplete" not in combined
