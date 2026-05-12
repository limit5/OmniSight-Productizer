"""OP-1018 tests for scripts/lint-jira-ticket-spec.py."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "lint-jira-ticket-spec.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("lint_jira_ticket_spec", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_markdown_devops_ticket_with_activation_bullet_passes(tmp_path) -> None:
    mod = _load_script()
    ticket = tmp_path / "jira-ticket-spec.md"
    ticket.write_text(
        "# Synthetic ticket\n\n"
        "Labels: area:devops area:tests\n\n"
        "## Acceptance Criteria\n"
        "- [ ] Operator activation: enable the deployed hook in pre-commit\n",
        encoding="utf-8",
    )

    assert mod.main([str(ticket)]) == 0


def test_markdown_devops_ticket_without_activation_bullet_fails(tmp_path, capsys) -> None:
    mod = _load_script()
    ticket = tmp_path / "jira-ticket-spec.md"
    ticket.write_text(
        "# Synthetic ticket\n\n"
        "Labels: area:devops area:tests\n\n"
        "## Acceptance Criteria\n"
        "- [ ] Add a lint hook\n",
        encoding="utf-8",
    )

    assert mod.main([str(ticket)]) == 1
    err = capsys.readouterr().err
    assert "area:devops ticket description lacks an activation bullet" in err


def test_python_ticket_spec_with_devops_activation_item_passes(tmp_path) -> None:
    mod = _load_script()
    ticket = tmp_path / "file_jira_ticket_specs.py"
    ticket.write_text(
        "from dataclasses import dataclass\n"
        "@dataclass\n"
        "class TicketSpec:\n"
        "    summary: str\n"
        "    code_ac: list[str]\n"
        "    deploy_ac: list[str]\n"
        "    integration_ac: list[str]\n"
        "    exercised_ac: list[str]\n"
        "    labels: list[str]\n"
        "SPEC = TicketSpec(\n"
        "    summary='Synthetic devops ticket',\n"
        "    code_ac=['Add a lint hook'],\n"
        "    deploy_ac=['Operator activation: install the pre-commit hook'],\n"
        "    integration_ac=[],\n"
        "    exercised_ac=[],\n"
        "    labels=['area:devops', 'area:tests'],\n"
        ")\n",
        encoding="utf-8",
    )

    assert mod.main([str(ticket)]) == 0


def test_python_ticket_spec_with_devops_missing_activation_item_fails(tmp_path, capsys) -> None:
    mod = _load_script()
    ticket = tmp_path / "file_jira_ticket_specs.py"
    ticket.write_text(
        "from dataclasses import dataclass\n"
        "@dataclass\n"
        "class TicketSpec:\n"
        "    summary: str\n"
        "    code_ac: list[str]\n"
        "    deploy_ac: list[str]\n"
        "    integration_ac: list[str]\n"
        "    exercised_ac: list[str]\n"
        "    labels: list[str]\n"
        "SPEC = TicketSpec(\n"
        "    summary='Synthetic devops ticket',\n"
        "    code_ac=['Add a lint hook'],\n"
        "    deploy_ac=['Register it in pre-commit'],\n"
        "    integration_ac=[],\n"
        "    exercised_ac=[],\n"
        "    labels=['area:devops', 'area:tests'],\n"
        ")\n",
        encoding="utf-8",
    )

    assert mod.main([str(ticket)]) == 1
    err = capsys.readouterr().err
    assert "area:devops TicketSpec lacks an activation AC bullet" in err
    assert "Synthetic devops ticket" in err
