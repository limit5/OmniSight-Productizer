"""FX.7.7 — drift guard for compose-file ``mem_limit`` + ``mem_reservation``.

Background
----------
The 2026-05-03 deep-audit row FX.7.7 asked us to add cgroup memory caps
to *every* service across all compose files. Two values must be
present together for each service:

    mem_limit:        kernel-enforced hard ceiling (cgroup memory.max)
    mem_reservation:  scheduler-honoured soft floor under host pressure

Setting only ``mem_limit`` lets the scheduler reclaim more memory than
the service can tolerate (eviction storms); setting only
``mem_reservation`` leaves the service with no hard ceiling, so a
runaway leak can starve the rest of the stack. Both knobs must be
present — that is the guard's contract.

What this test enforces
-----------------------
For every compose file in :data:`COMPOSE_FILES`, every service must
declare both ``mem_limit`` and ``mem_reservation`` keys with non-empty
values. The test also asserts the file *list* itself stays accurate —
if a new compose file lands without being added here, the auditor's
``find ... -name "docker-compose*.yml"`` sweep notices and flags it.

Why a YAML-load (not a string ``grep``)
---------------------------------------
A plain ``grep "mem_limit"`` in the compose file would pass for any
service whose YAML happens to *appear* in a comment block (the prod
file has ~10 such lines from FX.3.2 / threat-model documentation).
Loading the YAML and walking the ``services`` map is the only way to
say "service X actually has mem_limit set" instead of "the string
mem_limit appears somewhere on disk".
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]

COMPOSE_FILES: tuple[Path, ...] = (
    REPO_ROOT / "docker-compose.yml",
    REPO_ROOT / "docker-compose.prod.yml",
    REPO_ROOT / "docker-compose.staging.yml",
    REPO_ROOT / "docker-compose.test.yml",
    REPO_ROOT / "deploy" / "postgres-ha" / "docker-compose.yml",
)

REQUIRED_KEYS: tuple[str, ...] = ("mem_limit", "mem_reservation")


def _load_compose(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}
    assert isinstance(doc, dict), f"{path}: top-level must be a mapping"
    return doc


def test_all_compose_files_present() -> None:
    """Catches a new compose file that bypassed the audit list."""
    discovered = sorted(REPO_ROOT.rglob("docker-compose*.yml"))
    discovered = [
        p
        for p in discovered
        if "node_modules" not in p.parts and ".venv" not in p.parts
    ]
    listed = sorted(COMPOSE_FILES)
    missing_from_list = [p for p in discovered if p not in listed]
    assert not missing_from_list, (
        f"New compose file(s) not in COMPOSE_FILES — add them to FX.7.7 "
        f"drift guard: {[str(p.relative_to(REPO_ROOT)) for p in missing_from_list]}"
    )


@pytest.mark.parametrize("compose_path", COMPOSE_FILES, ids=lambda p: p.name)
def test_every_service_has_mem_limits(compose_path: Path) -> None:
    """Each service in each compose file must declare both knobs.

    Non-empty value (truthy YAML scalar) is required; an empty / null
    placeholder would parse but produce no cgroup setting at runtime.
    """
    doc = _load_compose(compose_path)
    services = doc.get("services") or {}
    assert services, f"{compose_path.name}: no services declared"

    violations: list[str] = []
    for name, body in services.items():
        if not isinstance(body, dict):
            violations.append(f"{name}: body is not a mapping")
            continue
        for key in REQUIRED_KEYS:
            value = body.get(key)
            if value is None or value == "":
                violations.append(f"{name}: missing or empty {key}")

    assert not violations, (
        f"{compose_path.relative_to(REPO_ROOT)} FX.7.7 violations:\n  - "
        + "\n  - ".join(violations)
        + "\nEvery service must declare both mem_limit AND mem_reservation."
    )


@pytest.mark.parametrize("compose_path", COMPOSE_FILES, ids=lambda p: p.name)
def test_mem_limit_values_are_canonical(compose_path: Path) -> None:
    """Reject suspicious values that would silently disable the cap.

    Compose accepts ``0`` and ``-1`` for some resource fields meaning
    "unlimited". For mem_limit / mem_reservation that defeats the
    purpose of FX.7.7, so we reject them here. Accept any positive
    string (e.g. ``2g``, ``512m``, ``1073741824``) — runtime parsing of
    the unit suffix is Compose's job, not this guard's.
    """
    doc = _load_compose(compose_path)
    services = doc.get("services") or {}

    bad: list[str] = []
    for name, body in services.items():
        if not isinstance(body, dict):
            continue
        for key in REQUIRED_KEYS:
            value = body.get(key)
            if value in (0, "0", -1, "-1"):
                bad.append(f"{name}.{key}={value!r} (means unlimited)")

    assert not bad, (
        f"{compose_path.relative_to(REPO_ROOT)} has cap-disabling values: "
        + ", ".join(bad)
    )
