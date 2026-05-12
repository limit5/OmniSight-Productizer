r"""[OP-974] AUDIT-19d — 5a → 5c staging migration runbook + portability audit.

Two things in one module, both in-area for this docs+tests ticket:

  1. **Runbook coverage** — pins that
     ``docs/operations/staging-migration-5a-to-5c-runbook.md`` actually
     contains the five things the ticket's acceptance criteria require
     (portability audit section, the WSL2 gotchas, a step-by-step
     migration, a verification test plan that re-runs the AUDIT-19a/b/c
     suites, and the DNS/firewall reachability section). This is what
     makes "migration runbook reviewed" / "operator can mental-dry-run"
     enforceable instead of vibes.

  2. **Portability audit** (AC #1 + the DoD "portability audit returns 0
     hits") — scans the AUDIT-19a/b/c artifacts under ``infra/staging/``,
     ``deploy/staging/`` and the staging ``deploy/systemd/*`` units for
     the four host-coupling anti-patterns and asserts none are present:

       * absolute host paths in compose bind mounts (must be relative or
         ``${VAR:-default}``; named volumes are fine; ``/var/run/docker.sock``
         is whitelisted — same path on every Linux host incl. WSL),
       * a hard-coded ``localhost:6432`` / pgbouncer host:port anywhere,
       * a bare literal host port in any ``ports:`` mapping (must be
         ``${STAGING_*_PORT:-default}:container``),
       * a cgroup ceiling in the compose systemd unit that is an absolute
         byte/CPU count rather than a percentage (so it auto-scales with
         the host),

     plus a guard that the unavoidable absolute paths in the systemd
     ``[Service]`` sections all share *one* prefix, so the migration's
     path rewrite is a single ``sed`` (runbook §3 step 5), not a
     per-file hunt.

  ``test_portability_audit_returns_zero_hits`` is the literal DoD gate:
  it runs every portability check and asserts the combined hit list is
  empty.

Cost: pure stdlib + PyYAML + pytest; no docker / systemd / network.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent

RUNBOOK = REPO_ROOT / "docs" / "operations" / "staging-migration-5a-to-5c-runbook.md"
STAGING_ENV_RUNBOOK = REPO_ROOT / "docs" / "operations" / "staging-environment-runbook.md"

COMPOSE = REPO_ROOT / "deploy" / "staging" / "docker-compose.yml"
CADDY_JSON = REPO_ROOT / "deploy" / "staging" / "caddy.json"
COMPOSE_UNIT = REPO_ROOT / "deploy" / "systemd" / "omnisight-staging-compose.service"
ENV_TEMPLATE = REPO_ROOT / "infra" / "staging" / ".env.template"

# Every systemd unit that activates a piece of the staging stack — these
# are what the migration's `sed` prefix-rewrite (runbook §3 step 5) walks.
STAGING_SYSTEMD_UNITS = sorted(
    p
    for p in (REPO_ROOT / "deploy" / "systemd").glob("*")
    if p.name == "omnisight-staging-compose.service" or p.name.startswith("staging-")
)

# Shell artifacts shipped by AUDIT-19a/b/c that the audit also scans for a
# hard-coded pgbouncer host:port.
STAGING_SHELL_ARTIFACTS = sorted((REPO_ROOT / "infra" / "staging").glob("*.sh"))

# Bind-mount sources that are the *same absolute path on every Linux host*
# (including a WSL2 distro) and therefore are not a 5a→5c portability hit.
PORTABLE_ABSOLUTE_MOUNTS = {"/var/run/docker.sock"}

# The single repo/host prefixes the systemd units are allowed to hard-code
# (an absolute `ExecStart=` is mandatory in systemd — there is no relative
# form). The migration rewrites exactly these three.
ALLOWED_SYSTEMD_HOST_PREFIXES = {
    "/home/user/sora-bridge",
    "/home/user/work",
    "/home/user/.config",
}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _read(path: Path) -> str:
    assert path.exists(), f"missing required artifact {path.relative_to(REPO_ROOT)}"
    return path.read_text(encoding="utf-8")


def _compose() -> dict:
    return yaml.safe_load(_read(COMPOSE))


def _service_block(unit_text: str) -> str:
    m = re.search(r"^\[Service\]\s*\n(.*?)(?=^\[|\Z)", unit_text, re.S | re.M)
    assert m, "unit file is missing a [Service] section"
    return m.group(1)


def _directive(block: str, key: str) -> str | None:
    m = re.search(rf"^{re.escape(key)}\s*=\s*(.+?)\s*$", block, re.M)
    return m.group(1) if m else None


def _mount_source(entry) -> str | None:
    """Host-side of a compose `volumes:` entry, or None for the long form /
    a `${...}`-templated source (templated == operator-overridable == fine)."""
    if isinstance(entry, dict):  # long-form {type:, source:, target:} — skip
        return None
    s = str(entry)
    if s.startswith("${"):
        return None
    return s.split(":", 1)[0]


# --------------------------------------------------------------------------- #
# portability audit — the individual checks (AC #1)
# --------------------------------------------------------------------------- #
def _hits_absolute_bind_mounts() -> list[str]:
    hits: list[str] = []
    for svc_name, svc in (_compose().get("services") or {}).items():
        for entry in svc.get("volumes", []) or []:
            src = _mount_source(entry)
            if src is None:
                continue
            if src.startswith(("./", "../")):  # relative — portable
                continue
            if not src.startswith("/"):  # named volume — portable
                continue
            if src in PORTABLE_ABSOLUTE_MOUNTS:
                continue
            hits.append(
                f"{COMPOSE.relative_to(REPO_ROOT)}: service '{svc_name}' bind-mounts "
                f"absolute host path '{src}' (use a relative path, a named volume, "
                f"or ${{VAR:-default}})"
            )
    # the bridge SSH key secret must also be env-templated
    secret = ((_compose().get("secrets") or {}).get("staging_gerrit_ssh_key") or {}).get("file", "")
    if secret and not str(secret).startswith("${"):
        hits.append(
            f"{COMPOSE.relative_to(REPO_ROOT)}: secret 'staging_gerrit_ssh_key' file "
            f"'{secret}' is not ${{VAR:-default}}-templated"
        )
    return hits


def _hits_pgbouncer_hardcode() -> list[str]:
    """AC #1: no hard-coded `localhost:6432` (pgbouncer) outside `.env.local`."""
    pat = re.compile(r"(?:localhost|127\.0\.0\.1|0\.0\.0\.0)\s*:\s*6432")
    hits: list[str] = []
    scanned = [COMPOSE, CADDY_JSON, COMPOSE_UNIT, ENV_TEMPLATE, *STAGING_SYSTEMD_UNITS, *STAGING_SHELL_ARTIFACTS]
    for path in scanned:
        if not path.exists():
            continue
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pat.search(line):
                hits.append(f"{path.relative_to(REPO_ROOT)}:{i}: hard-coded pgbouncer host:port — {line.strip()}")
    return hits


def _hits_compose_ports_not_via_env() -> list[str]:
    hits: list[str] = []
    for svc_name, svc in (_compose().get("services") or {}).items():
        for entry in svc.get("ports", []) or []:
            s = str(entry) if not isinstance(entry, dict) else str(entry.get("published", ""))
            if "${" not in s:
                hits.append(
                    f"{COMPOSE.relative_to(REPO_ROOT)}: service '{svc_name}' publishes port '{entry}' "
                    f"with a bare literal host port (use \"${{STAGING_*_PORT:-default}}:container\")"
                )
    return hits


def _hits_cgroup_not_fraction() -> list[str]:
    block = _service_block(_read(COMPOSE_UNIT))
    hits: list[str] = []
    for key in ("MemoryMax", "CPUQuota"):
        val = _directive(block, key)
        if val is None:
            hits.append(f"{COMPOSE_UNIT.relative_to(REPO_ROOT)}: missing {key}= in [Service]")
        elif not val.strip().endswith("%"):
            hits.append(
                f"{COMPOSE_UNIT.relative_to(REPO_ROOT)}: {key}={val} is an absolute value — "
                f"use a percentage so it auto-scales with the host"
            )
    return hits


def _hits_systemd_prefix_not_uniform() -> list[str]:
    """All absolute repo/log/config paths in the staging units must fall
    under one of a small fixed set of prefixes, so the migration rewrite is
    a single sed pass (runbook §3 step 5)."""
    hits: list[str] = []
    seen: set[str] = set()
    for unit in STAGING_SYSTEMD_UNITS:
        text = unit.read_text(encoding="utf-8")
        for m in re.finditer(r"/home/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", text):
            prefix = m.group(0)
            seen.add(prefix)
            if prefix not in ALLOWED_SYSTEMD_HOST_PREFIXES:
                hits.append(
                    f"{unit.relative_to(REPO_ROOT)}: hard-codes host path prefix '{prefix}' "
                    f"outside the migration-rewrite set {sorted(ALLOWED_SYSTEMD_HOST_PREFIXES)}"
                )
    # sanity: if a unit hard-codes nothing the test still passed; if it does,
    # `seen` is non-empty and a subset of the allowed set.
    assert seen, "expected the staging systemd units to hard-code at least one host path prefix"
    return hits


PORTABILITY_CHECKS = (
    ("absolute bind mounts", _hits_absolute_bind_mounts),
    ("pgbouncer host:port hardcode", _hits_pgbouncer_hardcode),
    ("compose ports not via env", _hits_compose_ports_not_via_env),
    ("cgroup limits not fractions", _hits_cgroup_not_fraction),
    ("systemd host-path prefixes not uniform", _hits_systemd_prefix_not_uniform),
)


# --------------------------------------------------------------------------- #
# portability audit — tests (AC #1 + DoD)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name,fn", PORTABILITY_CHECKS, ids=[n for n, _ in PORTABILITY_CHECKS])
def test_portability_check(name, fn):
    hits = fn()
    assert not hits, f"portability check '{name}' found {len(hits)} hit(s):\n" + "\n".join(f"  - {h}" for h in hits)


def test_portability_audit_returns_zero_hits():
    """The literal DoD gate: the whole audit must return 0 hits."""
    all_hits: list[str] = []
    for _name, fn in PORTABILITY_CHECKS:
        all_hits.extend(fn())
    assert not all_hits, "portability audit found hit(s):\n" + "\n".join(f"  - {h}" for h in all_hits)


def test_portability_compose_named_volumes_only():
    """Every top-level volume is a named docker volume (tar+scp portable),
    not a host-path bind — belt-and-braces over _hits_absolute_bind_mounts."""
    vols = _compose().get("volumes") or {}
    assert vols, "expected named volumes in the staging compose file"
    for vname, vdef in vols.items():
        # `driver: local` (or null) == named local volume; a `driver_opts`
        # with `o=bind`/`device=` would be a disguised bind mount.
        opts = (vdef or {}).get("driver_opts") or {}
        assert "device" not in opts, f"volume '{vname}' is a disguised bind mount via driver_opts"


# --------------------------------------------------------------------------- #
# runbook coverage — tests (AC #2 / #3 / #4 / #5 + DoD "reviewed / dry-runnable")
# --------------------------------------------------------------------------- #
def test_runbook_exists():
    assert RUNBOOK.exists(), f"missing {RUNBOOK.relative_to(REPO_ROOT)}"
    assert STAGING_ENV_RUNBOOK.exists(), "the 5a runbook this one builds on must exist"


def test_runbook_links_back_to_5a_runbook_and_ticket():
    text = _read(RUNBOOK)
    assert "staging-environment-runbook.md" in text, "must link back to the 5a runbook"
    assert "OP-974" in text and "AUDIT-19" in text, "must reference the ticket / META"


@pytest.mark.parametrize(
    "label,needles",
    [
        # AC #2 — WSL2 gotchas, all five sub-bullets:
        ("WSL2 networking — mirrored mode", ["mirrored", "networkingMode", "LAN"]),
        ("systemd in WSL2", ["systemd=true", "wsl.conf", "enable-linger"]),
        ("filesystem performance / avoid /mnt/c", ["/mnt/c", "9P", "ext4"]),
        ("time sync / clock drift breaks TLS", ["clock", "drift", "TLS", "timesyncd"]),
        ("network isolation needs firewall+VLAN, not docker net", ["VLAN", "firewall", "same LAN"]),
    ],
)
def test_runbook_covers_wsl_gotchas(label, needles):
    text = _read(RUNBOOK)
    missing = [n for n in needles if n.lower() not in text.lower()]
    assert not missing, f"runbook section '{label}' is missing mentions of: {missing}"


@pytest.mark.parametrize(
    "step",
    [
        "quiesce",          # stop 5a timers + compose unit before the copy
        "clone",            # get the repo onto 5c (Linux fs)
        ".env.template",    # recreate the host-side env file from the committed template
        "~/.config/omnisight",  # recreate the host-side secrets dir
        "daemon-reload",    # install + path-fix the systemd units on 5c
        "enable --now",     # bring the 5c stack up
        "rollback",         # documented rollback to 5a
        "decommission",     # tear down 5a only after a clean soak
    ],
)
def test_runbook_has_migration_step(step):
    assert step.lower() in _read(RUNBOOK).lower(), f"migration runbook is missing the '{step}' step"


def test_runbook_path_rewrite_uses_uniform_prefixes():
    """The §3 step-5 sed must rewrite exactly the prefixes the units use —
    keeps the runbook and the audit's ALLOWED_SYSTEMD_HOST_PREFIXES in sync."""
    text = _read(RUNBOOK)
    for prefix in ALLOWED_SYSTEMD_HOST_PREFIXES:
        assert prefix in text, f"runbook's path-rewrite step does not mention '{prefix}'"


def test_runbook_verification_plan_reruns_audit_19_suites():
    """AC #4 — the verification plan must name the AUDIT-19a/b/c (and this
    ticket's) test suites to re-run on 5c."""
    text = _read(RUNBOOK)
    for suite in (
        "test_staging_compose_unit.py",        # AUDIT-19a
        "test_staging_snapshot_restore.py",    # AUDIT-19b
        "test_staging_sync.py",                # AUDIT-19c
        "test_staging_migration_5a_to_5c.py",  # AUDIT-19d (this file)
    ):
        assert suite in text, f"verification test plan does not reference {suite}"


def test_runbook_covers_dns_and_firewall_reachability():
    """AC #5 — staging on 5c reachable from the prod-host milestone checker."""
    text = _read(RUNBOOK).lower()
    for needle in ("release_milestone_checker", "dns", "firewall", "staging.sora.services", "prod"):
        assert needle in text, f"DNS/firewall section is missing '{needle}'"
    # the actual probe path: gate units probe OMNISIGHT_STAGING_URL; the
    # checker reads the JSONL — the runbook must connect those.
    assert "omnisight_staging_url" in text, "runbook must explain that the gate units probe OMNISIGHT_STAGING_URL"
    assert "jsonl" in text, "runbook must explain the JSONL hand-off between the gate units and the checker"
