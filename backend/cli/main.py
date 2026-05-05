"""Click entry points for the ``omnisight`` CLI.

The group resolves :class:`CliConfig` once in the parent callback and
stuffs an :class:`OmniSightClient` into ``ctx.obj`` — tests substitute
a fake client via ``CliRunner(obj=...)`` without needing the network.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import click

from backend.agents import skills_loader
from backend.cli import formatters
from backend.cli.client import CliConfig, OmniSightCliError, OmniSightClient


VERSION = "0.1.0"


def _client_from_ctx(ctx: click.Context) -> OmniSightClient:
    obj = ctx.ensure_object(dict)
    existing = obj.get("client")
    if existing is None:
        cfg: CliConfig = obj["config"]
        existing = OmniSightClient(cfg)
        obj["client"] = existing
    return existing


def _emit(ctx: click.Context, payload: Any, text: str) -> None:
    """Write either ``--json`` payload or human text to stdout."""
    use_json = ctx.ensure_object(dict).get("json_output", False)
    if use_json:
        click.echo(formatters.format_json(payload))
    else:
        click.echo(text)


def _skills_registry_from_ctx(ctx: click.Context) -> skills_loader.SkillRegistry:
    """Load the effective WP.2 skill registry for local CLI inspection.

    Module-global state audit: this command keeps no global registry; each
    invocation derives the effective source from the same filesystem inputs.
    """
    obj = ctx.ensure_object(dict)
    existing = obj.get("skills_registry")
    if existing is not None:
        return existing
    project_root = Path(obj.get("project_root") or Path.cwd())
    home = obj.get("skills_home")
    registry = skills_loader.load_default_scopes(
        project_root,
        home=Path(home) if home is not None else None,
    )
    obj["skills_registry"] = registry
    return registry


def _skill_entry(registry: skills_loader.SkillRegistry, skill: skills_loader.Skill) -> dict[str, Any]:
    return {
        "name": skill.name,
        "description": skill.description,
        "keywords": list(skill.keywords),
        "scope": skill.scope,
        "provider_rank": registry.provider_rank(skill.name),
        "source_path": str(skill.source_path) if skill.source_path else "",
    }


@click.group(
    help="OmniSight CLI — terminal parity for the workspace + ChatOps surface.",
    context_settings={"help_option_names": ["-h", "--help"]},
)
@click.version_option(VERSION, prog_name="omnisight")
@click.option(
    "--base-url",
    envvar="OMNISIGHT_BASE_URL",
    default=None,
    help="Backend base URL. Default: http://localhost:8000 or $OMNISIGHT_BASE_URL.",
)
@click.option(
    "--token",
    envvar="OMNISIGHT_TOKEN",
    default=None,
    help="Bearer token for operator endpoints. Default: $OMNISIGHT_TOKEN.",
)
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    default=False,
    help="Emit machine-readable JSON instead of human text.",
)
@click.pass_context
def cli(ctx: click.Context, base_url: str | None, token: str | None, json_output: bool) -> None:
    obj = ctx.ensure_object(dict)
    # Tests pre-seed obj["config"] + obj["client"]; don't stomp if present.
    if "config" not in obj:
        obj["config"] = CliConfig.resolve(base_url, token)
    obj["json_output"] = json_output


# ─── status ────────────────────────────────────────────────────


@cli.command("status", help="Print system KPI snapshot (agents / workspaces / containers).")
@click.pass_context
def status_cmd(ctx: click.Context) -> None:
    client = _client_from_ctx(ctx)
    try:
        payload = client.status()
    except OmniSightCliError as exc:
        raise click.ClickException(str(exc))
    _emit(ctx, payload, formatters.format_status(payload))


# ─── workspace list ───────────────────────────────────────────


@cli.group("workspace", help="Inspect agent workspaces.")
def workspace_group() -> None:
    pass


@workspace_group.command("list", help="List active agent workspaces.")
@click.pass_context
def workspace_list_cmd(ctx: click.Context) -> None:
    client = _client_from_ctx(ctx)
    try:
        rows = client.list_workspaces()
    except OmniSightCliError as exc:
        raise click.ClickException(str(exc))
    _emit(ctx, rows, formatters.format_workspace_list(rows))


# ─── skills list / resolve ────────────────────────────────────


@cli.group("skills", help="Inspect effective WP.2 skill sources.")
def skills_group() -> None:
    pass


@skills_group.command("list", help="List effective skills and source paths.")
@click.pass_context
def skills_list_cmd(ctx: click.Context) -> None:
    registry = _skills_registry_from_ctx(ctx)
    rows = [_skill_entry(registry, skill) for skill in registry.list_all()]
    _emit(ctx, rows, formatters.format_skills_list(rows))


@skills_group.command("resolve", help="Show the effective source for NAME.")
@click.argument("name")
@click.pass_context
def skills_resolve_cmd(ctx: click.Context, name: str) -> None:
    registry = _skills_registry_from_ctx(ctx)
    skill = registry.get(name)
    if skill is None:
        raise click.ClickException(f"skill {name!r} not found")
    row = _skill_entry(registry, skill)
    _emit(ctx, row, formatters.format_skill_resolve(row))


# ─── run (NL prompt → /invoke/stream) ─────────────────────────


@cli.command("run", help="Drive /invoke/stream with a natural-language prompt.")
@click.argument("prompt", nargs=-1, required=True)
@click.option(
    "--quiet",
    is_flag=True,
    default=False,
    help="Only print the final result event, not each interim frame.",
)
@click.pass_context
def run_cmd(ctx: click.Context, prompt: tuple[str, ...], quiet: bool) -> None:
    text = " ".join(prompt).strip()
    if not text:
        raise click.UsageError("PROMPT is required")
    client = _client_from_ctx(ctx)
    obj = ctx.ensure_object(dict)
    use_json = obj.get("json_output", False)
    collected: list[dict[str, Any]] = []
    try:
        for event, data in client.run_stream(text):
            frame = {"event": event, "data": data}
            collected.append(frame)
            if use_json:
                # Stream one JSON object per line — caller can `| jq -s`.
                click.echo(formatters.format_json(frame))
            elif not quiet:
                click.echo(formatters.format_run_event(event, data))
    except OmniSightCliError as exc:
        raise click.ClickException(str(exc))
    if quiet and not use_json and collected:
        # Emit only the last non-keepalive frame as the "result".
        last = collected[-1]
        click.echo(formatters.format_run_event(last["event"], last["data"]))


# ─── inspect ──────────────────────────────────────────────────


@cli.command("inspect", help="Show agent detail + workspace pointer for AGENT_ID.")
@click.argument("agent_id")
@click.pass_context
def inspect_cmd(ctx: click.Context, agent_id: str) -> None:
    client = _client_from_ctx(ctx)
    try:
        agent = client.get_agent(agent_id)
        workspace = client.get_workspace(agent_id)
    except OmniSightCliError as exc:
        raise click.ClickException(str(exc))
    payload = {"agent": agent, "workspace": workspace}
    _emit(ctx, payload, formatters.format_inspect(agent, workspace))


# ─── inject ───────────────────────────────────────────────────


@cli.command("inject", help="Inject an operator hint into the agent's blackboard.")
@click.argument("agent_id")
@click.argument("hint", nargs=-1, required=True)
@click.option("--author", default="cli", show_default=True, help="Author tag for audit log.")
@click.pass_context
def inject_cmd(ctx: click.Context, agent_id: str, hint: tuple[str, ...], author: str) -> None:
    text = " ".join(hint).strip()
    if not text:
        raise click.UsageError("HINT text is required")
    client = _client_from_ctx(ctx)
    try:
        payload = client.inject_hint(agent_id, text, author=author)
    except OmniSightCliError as exc:
        raise click.ClickException(str(exc))
    _emit(ctx, payload, formatters.format_inject_result(payload))


def main(argv: list[str] | None = None) -> int:
    """Console-script entry point.

    Returns the exit code so tests can call ``main([...])`` directly
    instead of going through ``sys.exit``.
    """
    try:
        cli.main(args=argv if argv is not None else sys.argv[1:], standalone_mode=False)
        return 0
    except click.exceptions.ClickException as exc:
        exc.show()
        return exc.exit_code
    except click.exceptions.Abort:
        click.echo("Aborted.", err=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
