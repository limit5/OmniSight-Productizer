"""BS.4.7 — installer sidecar test fixtures.

Two responsibilities:

1. Pin the repo root onto ``sys.path`` so ``import installer.*``
   resolves whether pytest was launched from the repo root or from
   the ``installer/`` subdir. The sidecar is intentionally NOT a
   pip-installable package (Dockerfile.installer just COPYs the
   source), so a setup.cfg / pyproject editable install is overkill
   for unit tests.

2. Reset module-global env state between tests. The sidecar reads
   ``OMNISIGHT_INSTALLER_AIRGAP`` / ``OMNISIGHT_INSTALLER_TOKEN`` /
   etc. at call time (not at import time — see the audit notes in
   ``installer/main.py`` and ``installer/methods/base.py``), but a
   leaked env var between tests still breaks isolation. We snapshot
   ``os.environ`` and restore it on teardown.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Make ``installer.*`` importable regardless of pytest's cwd.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


@pytest.fixture(autouse=True)
def _isolate_installer_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip every ``OMNISIGHT_INSTALLER_*`` env var before each test
    so a previous test's airgap=1 / TOKEN=xxx setting can't leak.

    monkeypatch handles the unwind on test teardown.
    """
    for key in [k for k in os.environ if k.startswith("OMNISIGHT_INSTALLER_")]:
        monkeypatch.delenv(key, raising=False)
    # Also strip the ambiguous OMNISIGHT_AUTH_MODE / DECISION_BEARER
    # that some methods consult indirectly via the backend (we never
    # actually hit the backend in these tests, but the env-poisoning
    # surface is small enough to clean defensively).
    for key in ("OMNISIGHT_AUTH_MODE", "OMNISIGHT_DECISION_BEARER"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def isolated_toolchains_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> Path:
    """Redirect the methods' filesystem layout into ``tmp_path`` so
    scratch dirs and atomic-promote targets land in test scope, not
    ``/var/lib/omnisight/toolchains/``.

    The constants are read at call time inside ``scratch_path_for_job``
    / ``entry_install_root`` (see base.py) so monkeypatching the module
    attr is sufficient — no import-order wrinkle.
    """
    root = tmp_path / "toolchains"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        "installer.methods.base.TOOLCHAINS_ROOT", str(root),
    )
    return root
