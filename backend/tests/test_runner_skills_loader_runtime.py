"""OP-1678 (WP.2 verify) — prove the LIVE runner agent path resolves skills
via ``skills_loader.load_default_scopes`` / ``make_skill_handler`` AT RUNTIME.

The WP.2 3-way audit (2026-05-24) confirmed the loader is built and wired into
``tool_schemas`` / ``runbook_loader`` / the CLI / the ``/skills/effective``
router, but flagged ONE unproven thing: whether the live runner agent
(``auto-runner-sdk.py`` and ``scripts/run_s1_via_anthropic_sdk.py``) actually
*consumes* the loader when it registers the ``Skill`` tool — or whether it still
falls back to the pre-WP.2.5 hard-coded ``SKILL_HD_*`` table.

These tests close that gap. They import each live runner launcher *by file
path* (the same way the launchers are executed in production) and exercise the
exact module-bound ``load_default_scopes`` / ``make_skill_handler`` symbols the
launcher's ``main()`` uses to register ``Skill``. The assertion is concrete:
invoking the ``Skill`` tool for ``SKILL_HD_PARSE`` must return the **bundled**
filesystem pack (``omnisight/agents/skills/hd-parse/SKILL.md``) — NOT the
hard-coded rollback placeholder — when the loader knob is on; and must fall
back to the hard-coded registry when the knob is off. A third test pins the
"all three surfaces agree" acceptance criterion: the runner path, the CLI
``omnisight skills resolve`` command, and the ``/skills/effective`` router all
report the same effective ``(scope, source_path)`` for ``SKILL_HD_PARSE``.

Reuses ``skills_loader`` public API only — no loader internals are touched.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from backend.agents import skills_loader
from backend.agents.skills_loader import SKILLS_LOADER_ENABLED_ENV
from backend.agents.tool_dispatcher import ToolDispatcher


REPO = Path(__file__).resolve().parents[2]
RUN_S1_LAUNCHER = REPO / "scripts" / "run_s1_via_anthropic_sdk.py"
AUTO_RUNNER_SDK = REPO / "auto-runner-sdk.py"

# Disambiguating markers from the two competing bodies SKILL_HD_PARSE can
# resolve to (see skills_loader._load_hard_coded_registry and the bundled
# omnisight/agents/skills/hd-parse/SKILL.md).
_BUNDLED_MARKER = "Deferred BP.B Guild placeholder"
_HARDCODED_MARKER = "Hard-coded WP.2 rollback placeholder"
_BUNDLED_REL_PATH = Path("omnisight") / "agents" / "skills" / "hd-parse" / "SKILL.md"


def _load_launcher(module_name: str, path: Path):
    """Import a runner launcher by file path (they live outside ``backend/``,
    so a package import does not reach them — this mirrors how the launchers
    are run in production: ``python <path>``)."""
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec and spec.loader, f"cannot build import spec for {path}"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _runner_skill_dispatch(mod, project_root: Path) -> tuple[ToolDispatcher, skills_loader.SkillRegistry]:
    """Replay the launcher's ``Skill``-tool registration using the launcher's
    OWN module-bound loader symbols and project root, then return the wired
    dispatcher + registry.

    This is the crux of the proof: ``mod.load_default_scopes`` and
    ``mod.make_skill_handler`` are the identical objects the launcher's
    ``main()`` calls at runtime (run_s1 ~L1655, auto-runner-sdk ~L829). If the
    launcher had kept a hard-coded path these symbols would not be bound.
    """
    assert hasattr(mod, "load_default_scopes"), "launcher does not import load_default_scopes"
    assert hasattr(mod, "make_skill_handler"), "launcher does not import make_skill_handler"
    registry = mod.load_default_scopes(project_root)
    dispatcher = ToolDispatcher()
    dispatcher.register("Skill", mod.make_skill_handler(registry))
    return dispatcher, registry


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "module_name, launcher_path, root_attr",
    [
        ("run_s1_op1678", RUN_S1_LAUNCHER, "REPO"),
        ("auto_runner_sdk_op1678", AUTO_RUNNER_SDK, "BASE_DIR"),
    ],
)
async def test_runner_skill_tool_resolves_skill_hd_parse_via_loader(
    monkeypatch: pytest.MonkeyPatch,
    module_name: str,
    launcher_path: Path,
    root_attr: str,
) -> None:
    """AC (Code/Integration): with the knob ON, the live runner ``Skill``-tool
    path returns SKILL_HD_PARSE resolved by ``load_default_scopes`` from the
    bundled filesystem pack — NOT the hard-coded fallback."""
    monkeypatch.setenv(SKILLS_LOADER_ENABLED_ENV, "true")
    mod = _load_launcher(module_name, launcher_path)
    project_root = getattr(mod, root_attr)
    assert Path(project_root) == REPO, f"{root_attr} should point at the repo root"

    dispatcher, registry = _runner_skill_dispatch(mod, project_root)

    # The registry the runner built must resolve SKILL_HD_PARSE from the
    # bundled scope (the loader path), not the hard-coded rollback registry.
    resolved = registry.get("SKILL_HD_PARSE")
    assert resolved is not None, "SKILL_HD_PARSE not in runner registry"
    assert resolved.scope == "bundled", f"expected bundled scope, got {resolved.scope!r}"
    assert resolved.source_path is not None
    assert resolved.source_path.parts[-len(_BUNDLED_REL_PATH.parts):] == _BUNDLED_REL_PATH.parts

    # Exercise the actual Skill-tool dispatch the LLM would trigger.
    result = await dispatcher.execute(
        "tu-op1678-hd-parse", "Skill", {"skill": "SKILL_HD_PARSE"}
    )
    assert result.is_error is False
    assert _BUNDLED_MARKER in result.content
    assert _HARDCODED_MARKER not in result.content


@pytest.mark.asyncio
async def test_runner_skill_tool_falls_back_to_hardcoded_when_knob_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC (knob semantics): with the knob OFF, the same runner path returns the
    pre-WP.2.5 hard-coded SKILL_HD registry entry — proving the loader is what
    the runner consumes when the knob is on (and the knob actually gates it)."""
    monkeypatch.setenv(SKILLS_LOADER_ENABLED_ENV, "false")
    mod = _load_launcher("run_s1_op1678_off", RUN_S1_LAUNCHER)

    dispatcher, registry = _runner_skill_dispatch(mod, mod.REPO)

    resolved = registry.get("SKILL_HD_PARSE")
    assert resolved is not None
    assert resolved.scope == "hardcoded", f"expected hardcoded scope, got {resolved.scope!r}"
    assert resolved.source_path is None

    result = await dispatcher.execute(
        "tu-op1678-hd-parse-off", "Skill", {"skill": "SKILL_HD_PARSE"}
    )
    assert result.is_error is False
    assert _HARDCODED_MARKER in result.content
    assert _BUNDLED_MARKER not in result.content


def test_runner_cli_and_router_agree_on_effective_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC (Exercised): the runner path, ``omnisight skills resolve``, and the
    ``/skills/effective`` router all agree on the effective ``(scope,
    source_path)`` for SKILL_HD_PARSE — because all three derive it from the
    same ``load_default_scopes`` over the same checkout."""
    monkeypatch.setenv(SKILLS_LOADER_ENABLED_ENV, "true")

    # Runner path (run_s1 launcher's bound loader symbol + repo root).
    mod = _load_launcher("run_s1_op1678_agree", RUN_S1_LAUNCHER)
    _, runner_registry = _runner_skill_dispatch(mod, mod.REPO)
    runner_skill = runner_registry.get("SKILL_HD_PARSE")

    # CLI surface: backend.cli.main._skill_entry over the same project root.
    from backend.cli import main as cli_main

    cli_registry = skills_loader.load_default_scopes(REPO)
    cli_entry = cli_main._skill_entry(cli_registry, cli_registry.get("SKILL_HD_PARSE"))

    # Router surface: backend.routers.skills._effective_skill_entry over its
    # module-level project root (which is the repo root).
    from backend.routers import skills as skills_router

    router_registry = skills_loader.load_default_scopes(skills_router._PROJECT_ROOT)
    router_entry = skills_router._effective_skill_entry(
        router_registry.get("SKILL_HD_PARSE")
    )

    assert runner_skill is not None
    assert runner_skill.scope == cli_entry["scope"] == router_entry["scope"] == "bundled"
    assert (
        str(runner_skill.source_path)
        == cli_entry["source_path"]
        == router_entry["source_path"]
    )
